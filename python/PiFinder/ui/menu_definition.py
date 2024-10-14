from dataclasses import dataclass, field
from typing import Type, Any
from PiFinder.composite_object import CompositeObject


@dataclass
class MenuItemDefinition:

    # The name of the menu item
    name: str = field(default="")
    menu_class: Type = field(default=object)
    stateful: bool = field(default=False)
    state: Any = field(default=None)
    preload: bool = field(default=False)
    select: str = field(default="")
    items: list = field(default_factory=list)
    objects: list[CompositeObject] = field(default_factory=list)
    label: str = field(default="")
    sorting: list[str] = field(default_factory=list)
    enabled: bool = field(default=True)

    def __eq__(self, other):
        if not isinstance(other, MenuItemDefinition):
            return NotImplemented
        return self.name == other.name and self.label == other.label

    def __hash__(self):
        return hash(self.name+self.label)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    @property
    def display_name(self):
        """
        Returns the display name for this object
        """
        return f"{self.name}"
