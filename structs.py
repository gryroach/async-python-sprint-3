import json

from dataclasses import dataclass, asdict, field
from typing import Literal


@dataclass
class RequestData:
    username: str
    target: Literal['all', 'one_to_one', 'hello', 'status'] = 'all'
    receiver: str = ''
    message: str = ''

    def to_json(self):
        return asdict(self)

    def to_string_json(self):
        return json.dumps(self.to_json())
