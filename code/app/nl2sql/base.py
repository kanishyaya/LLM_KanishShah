"""Common interface every NL -> SQL engine implements."""
from abc import ABC, abstractmethod


class UnanswerableQuestionError(Exception):
    """Raised when the engine determines the question cannot be answered
    from the available schema (ambiguous, out-of-scope, or unsupported)."""


class NL2SQLEngine(ABC):
    @abstractmethod
    def generate_sql(self, question: str) -> str:
        """Return a single SQL SELECT string for the given question.

        Implementations should raise UnanswerableQuestionError (rather than
        guessing) when the question can't be confidently mapped to the
        schema.
        """
        raise NotImplementedError
