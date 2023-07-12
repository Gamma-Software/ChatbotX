"""Chain for chatting with a vector database."""
from __future__ import annotations

import re
import inspect
import warnings
from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from pydantic import Extra, Field, root_validator

from langchain.base_language import BaseLanguageModel
from langchain.callbacks.manager import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
    Callbacks,
)
from langchain.chains.base import Chain
from langchain.chains.combine_documents.base import BaseCombineDocumentsChain
from langchain.chains.combine_documents.stuff import StuffDocumentsChain
from langchain.chains.llm import LLMChain
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts.base import BasePromptTemplate
from langchain.schema import BaseRetriever, Document

from langchain.chains.conversational_retrieval.base import CHAT_TURN_TYPE, _get_chat_history

from chains.prompt import CONDENSE_QUESTION_CODE_PROMPT, PROMPT, prompt_instruct_check


# Should create a generic parser for this
def parse_code(output):
    python_code = None
    explain_code = None
    pattern = r"```python(.*?)```(.*?)$"
    python_code_match = re.search(pattern, output, re.DOTALL)
    if python_code_match:
        python_code = python_code_match.group(1)
        explain_code = python_code_match.group(2)
        if python_code == "None":
            python_code = None
    return python_code, explain_code

class BaseConversationalRetrievalCodeChain(Chain):
    """Chain for chatting with an index. Given the chat history, the current code and a question, return the answer."""

    combine_docs_chain: BaseCombineDocumentsChain
    question_generator: LLMChain
    output_key: List[str] = ["code", "explanation"]
    constitutional_chain: LLMChain
    return_source_documents: bool = False
    return_generated_question: bool = False
    return_revision_request: bool = False
    get_chat_history: Optional[Callable[[CHAT_TURN_TYPE], str]] = None
    """Return the source documents."""

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True
        allow_population_by_field_name = True

    @property
    def input_keys(self) -> List[str]:
        """Input keys."""
        return ["question"]

    @property
    def output_keys(self) -> List[str]:
        """Return the output keys.

        :meta private:
        """
        _output_keys = ["code", "explanation"]
        if self.return_source_documents:
            _output_keys = _output_keys + ["source_documents"]
        if self.return_generated_question:
            _output_keys = _output_keys + ["generated_question"]
        return _output_keys

    @abstractmethod
    def _get_docs(
        self,
        question: str,
        inputs: Dict[str, Any],
        *,
        run_manager: CallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        _run_manager = run_manager or CallbackManagerForChainRun.get_noop_manager()
        request = inputs["question"]
        new_request = self.question_generator.run(
            question=request, callbacks=_run_manager.get_child()
        )
        accepts_run_manager = (
            "run_manager" in inspect.signature(self._get_docs).parameters
        )
        if accepts_run_manager:
            docs = self._get_docs(new_request, inputs, run_manager=_run_manager)
        else:
            docs = self._get_docs(new_request, inputs)  # type: ignore[call-arg]
        new_inputs = inputs.copy()
        # Remove any mentions of streamlit or python from the question
        new_request = new_request.replace("streamlit", "").replace("python", "")
        get_chat_history = self.get_chat_history or _get_chat_history
        chat_history_str = get_chat_history(inputs["chat_history"])
        new_inputs["chat_history"] = chat_history_str
        answer = self.combine_docs_chain.run(
            input_documents=docs, callbacks=_run_manager.get_child(), **new_inputs
        )
        code, expl = parse_code(answer)

        is_code_not_safe = True
        if code is not None:
            # Run check code
            is_code_not_safe = self.constitutional_chain.run(code=code)

        output: Dict[str, Any] = {self.output_key[0]: code, self.output_key[1]: expl}
        if self.return_source_documents:
            output["source_documents"] = docs
        if self.return_generated_question:
            output["generated_question"] = new_request
        if self.return_revision_request:
            output["revision_request"] = is_code_not_safe
        return output

    @abstractmethod
    async def _aget_docs(
        self,
        question: str,
        inputs: Dict[str, Any],
        *,
        run_manager: AsyncCallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""

    async def _acall(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[AsyncCallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        _run_manager = run_manager or AsyncCallbackManagerForChainRun.get_noop_manager()
        request = inputs["question"]
        new_request = await self.question_generator.arun(
            question=request, callbacks=_run_manager.get_child()
        )
        accepts_run_manager = (
            "run_manager" in inspect.signature(self._aget_docs).parameters
        )
        if accepts_run_manager:
            docs = await self._aget_docs(new_request, inputs, run_manager=_run_manager)
        else:
            docs = await self._aget_docs(new_request, inputs)  # type: ignore[call-arg]

        new_inputs = inputs.copy()
        # Remove any mentions of streamlit or python from the question
        new_request = new_request.replace("streamlit", "").replace("python", "")
        get_chat_history = self.get_chat_history or _get_chat_history
        chat_history_str = get_chat_history(inputs["chat_history"])
        new_inputs["chat_history"] = chat_history_str
        answer = await self.combine_docs_chain.arun(
            input_documents=docs, callbacks=_run_manager.get_child(), **new_inputs
        )
        code, expl = parse_code(answer)

        is_code_not_safe = True
        if code is not None:
            # Run check code
            is_code_not_safe = self.constitutional_chain.run(code=code)

        output: Dict[str, Any] = {self.output_key[0]: code, self.output_key[1]: expl}
        if self.return_source_documents:
            output["source_documents"] = docs
        if self.return_generated_question:
            output["generated_question"] = new_request
        if self.return_revision_request:
            output["revision_request"] = is_code_not_safe
        return output

    def save(self, file_path: Union[Path, str]) -> None:
        if self.get_chat_history:
            raise ValueError("Chain not savable when `get_chat_history` is not None.")
        super().save(file_path)


class ConversationalRetrievalCodeChain(BaseConversationalRetrievalCodeChain):
    """Chain for chatting with an index."""

    retriever: BaseRetriever
    """Index to connect to."""
    max_tokens_limit: Optional[int] = None
    """If set, restricts the docs to return from store based on tokens, enforced only
    for StuffDocumentChain"""

    def _reduce_tokens_below_limit(self, docs: List[Document]) -> List[Document]:
        num_docs = len(docs)

        if self.max_tokens_limit and isinstance(
            self.combine_docs_chain, StuffDocumentsChain
        ):
            tokens = [
                self.combine_docs_chain.llm_chain.llm.get_num_tokens(doc.page_content)
                for doc in docs
            ]
            token_count = sum(tokens[:num_docs])
            while token_count > self.max_tokens_limit:
                num_docs -= 1
                token_count -= tokens[num_docs]

        return docs[:num_docs]

    def _get_docs(
        self,
        question: str,
        inputs: Dict[str, Any],
        *,
        run_manager: CallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""
        docs = self.retriever.get_relevant_documents(
            question, callbacks=run_manager.get_child()
        )
        return self._reduce_tokens_below_limit(docs)

    async def _aget_docs(
        self,
        question: str,
        inputs: Dict[str, Any],
        *,
        run_manager: AsyncCallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""
        docs = await self.retriever.aget_relevant_documents(
            question, callbacks=run_manager.get_child()
        )
        return self._reduce_tokens_below_limit(docs)

    @classmethod
    def from_llm(
        cls,
        llm: BaseLanguageModel,
        retriever: BaseRetriever,
        condense_question_prompt: BasePromptTemplate = CONDENSE_QUESTION_CODE_PROMPT,
        chain_type: str = "stuff",
        verbose: bool = False,
        condense_question_llm: Optional[BaseLanguageModel] = None,
        self_critique_llm: Optional[BaseLanguageModel] = None,
        combine_docs_chain_kwargs: Optional[Dict] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> BaseConversationalRetrievalCodeChain:
        """Load chain from LLM."""
        combine_docs_chain_kwargs = combine_docs_chain_kwargs or {"prompt": PROMPT}

        doc_chain = load_qa_chain(
            llm,
            chain_type=chain_type,
            verbose=verbose,
            callbacks=callbacks,
            **combine_docs_chain_kwargs,
        )

        _llm = condense_question_llm or llm
        condense_question_chain = LLMChain(
            llm=_llm,
            prompt=condense_question_prompt,
            verbose=verbose,
            callbacks=callbacks,
        )

        _llm_2 = self_critique_llm or llm
        check_code_chain = LLMChain(llm=_llm_2, prompt=prompt_instruct_check)

        return cls(
            retriever=retriever,
            combine_docs_chain=doc_chain,
            constitutional_chain=check_code_chain,
            question_generator=condense_question_chain,
            callbacks=callbacks,
            **kwargs,
        )