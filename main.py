from typing import Dict, List

from chromadb.api.models.Collection import Collection
from langchain import LLMChain, BasePromptTemplate
from langchain.chains.base import Chain
from langchain.chains.conversation.prompt import PROMPT
from langchain.input import get_color_mapping
from langchain.llms import OpenAI
from langchain.memory import ConversationBufferMemory, ConversationBufferWindowMemory
from langchain.schema import BaseMemory
from pydantic import Extra, Field, root_validator
from prompt_config import Config
import re


class ConversationalRouterChain(LLMChain):
    """Router chain that picks the most relevant model to call based on vector queries.
    The chain also has inherent memory for conversational chat applications"""

    memory: BaseMemory = Field(default_factory=ConversationBufferMemory)
    """Default memory store."""
    prompt: BasePromptTemplate = PROMPT
    """Default conversation prompt to use."""
    last_chain: Chain = None
    chains: Dict[str, Chain]
    strip_outputs: bool = False
    input_key: str = "input"  #: :meta private:
    output_key: str = "output"  #: :meta private:
    vector_collection: Collection = None

    class Config:
        """Configuration for this pydantic object."""
        extra = Extra.forbid
        arbitrary_types_allowed = True

    @property
    def input_keys(self) -> List[str]:
        """Expect input key.

        :meta private:
        """
        return [self.input_key]

    @property
    def output_keys(self) -> List[str]:
        """Return output key.

        :meta private:
        """
        return [self.output_key]

    def _call(self, inputs: Dict[str, str]) -> Dict[str, str]:
        _input = inputs[self.input_key]
        # weird hack to extract previous model used in the response.
        # There was no way I could see to know the attribution of the model to the response in history.
        last_chain_name = re.compile('<chain>(.*?)</chain>').findall(inputs['history'])
        if last_chain_name and len(last_chain_name) > 0:
            self.last_chain = self.chains.get(last_chain_name[0])
        color_mapping = get_color_mapping([str(x) for x in self.chains.keys()])
        if not self.vector_collection:
            raise ValueError("Router embeddings in SimpleRouterPipeline is empty or not provided.")
        x = self.vector_collection.query(query_texts=[_input], n_results=3)
        classification, distance = x['metadatas'][0][0], x['distances'][0][0]
        # print(classification, distance)
        mname = classification['classification']
        # picking a guardrail where if the AI response is way off - then just use the same model as the previous
        # one to continue conversing.
        if self.chains.get(classification['classification']) and distance <= 1.5:
            _input = self.chains[classification['classification']](_input)
        else:
            if self.last_chain:
                mname = last_chain_name[0]
                _input = self.last_chain(_input)
            else:
                raise ValueError("Suitable destination chain not found for %s type" % classification['classification'])
        self.callback_manager.on_text(
            str(_input['text']), color=color_mapping[mname], end="\n", verbose=self.verbose
        )
        return {self.output_key: '<chain>' + classification['classification'] + '</chain>' + _input['text']}

    @root_validator()
    def validate_prompt_input_variables(cls, values: Dict) -> Dict:
        """Validate that prompt input variables are consistent."""
        memory_keys = values["memory"].memory_variables
        input_key = values["input_key"]
        if input_key in memory_keys:
            raise ValueError(
                f"The input key {input_key} was also found in the memory keys "
                f"({memory_keys}) - please provide keys that don't overlap."
            )
        prompt_variables = values["prompt"].input_variables
        expected_keys = memory_keys + [input_key]
        if set(expected_keys) != set(prompt_variables):
            raise ValueError(
                "Got unexpected prompt input variables. The prompt expects "
                f"{prompt_variables}, but got {memory_keys} as inputs from "
                f"memory, and {input_key} as the normal input key."
            )
        return values


if __name__ == "__main__":
    # set up LLM
    llm = OpenAI(temperature=0.3)
    # define chain map - add any model here.
    c = Config()
    # set up router chain
    router_chain = ConversationalRouterChain(llm=llm, chains=c.get_chains(), vector_collection=c.get_embedding(),
                                             memory=ConversationBufferWindowMemory(k=1), verbose=True)
    # inference
    while True:
        text = input()
        output = router_chain.predict(input=text)
