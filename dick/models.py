from dataclasses import asdict, dataclass


class DickError(Exception):
    pass


@dataclass(frozen=True)
class Package:
    name: str
    source: str
    description: str = ""
    version: str = ""
    repository: str = ""
    architecture: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class LocalPackage:
    source: str
    name: str
    version: str = ""
    description: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Repository:
    source: str
    name: str
    urls: tuple[str, ...]
    architecture: str = ""
    suite: str = ""
    component: str = ""
    mirrorlist: str = ""
    metalink: str = ""

    @property
    def key(self):
        return f"{self.source}:{self.name}"
