from abc import ABC, abstractmethod
from typing import Tuple,List,Dict


class BaseModel(ABC):

    @abstractmethod
    def chat(self, messages: list[dict]) -> str:
        pass


