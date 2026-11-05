"""Loading a student protocol.

A protocol is a Python module that defines a Sender subclass and a Receiver
subclass and exports them:

    SENDER, RECEIVER = MySender, MyReceiver

Specs accepted:
    example/baseline_stopwait/protocol.py  a file path
    example.baseline_stopwait.protocol     an importable module
    path/or/module:Sender,Receiver         explicit class names
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .api import Receiver, Sender


class ProtocolLoadError(RuntimeError):
    pass


def _import_path(path: Path, name: str | None = None) -> ModuleType:
    name = name or f"student_protocol_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ProtocolLoadError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    # Registering before exec so a protocol split across files can import its
    # own helpers by module name.
    sys.modules[name] = module
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def load_module(spec: str, module_name: str | None = None) -> tuple[ModuleType, list[str]]:
    """Import a protocol. `module_name` loads a fresh copy under that name, which
    is how the ns-3 runner gives every host its own module globals."""
    target, _, names = spec.partition(":")
    explicit = [n for n in names.split(",") if n]
    path = Path(target)
    if path.suffix == ".py" or path.is_file():
        if not path.is_file():
            raise ProtocolLoadError(f"no such protocol file: {path}")
        return _import_path(path, module_name), explicit
    if path.is_dir():
        candidate = path / "protocol.py"
        if not candidate.is_file():
            raise ProtocolLoadError(f"{path} has no protocol.py")
        return _import_path(candidate, module_name), explicit
    if module_name is not None:
        found = importlib.util.find_spec(target)
        if found is None or found.origin is None:
            raise ProtocolLoadError(f"cannot import protocol {target!r}")
        return _import_path(Path(found.origin), module_name), explicit
    try:
        return importlib.import_module(target), explicit
    except ImportError as exc:
        raise ProtocolLoadError(f"cannot import protocol {target!r}: {exc}") from None


def _subclasses(module: ModuleType, base: type) -> list[type]:
    found = []
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, base) and value is not base:
            # A Sender is also an Endpoint, so keep the pair from crossing over.
            if base is Sender and issubclass(value, Receiver):
                continue
            if base is Receiver and issubclass(value, Sender):
                continue
            found.append(value)
    return found


def load(spec: str, module_name: str | None = None) -> tuple[type, type]:
    """Return (sender_class, receiver_class) for a protocol spec."""
    module, explicit = load_module(spec, module_name)

    if explicit:
        if len(explicit) != 2:
            raise ProtocolLoadError("explicit class names must be Sender,Receiver")
        try:
            return getattr(module, explicit[0]), getattr(module, explicit[1])
        except AttributeError as exc:
            raise ProtocolLoadError(f"{spec}: {exc}") from None

    sender = getattr(module, "SENDER", None)
    receiver = getattr(module, "RECEIVER", None)
    if sender is None or receiver is None:
        senders = _subclasses(module, Sender)
        receivers = _subclasses(module, Receiver)
        if len(senders) != 1 or len(receivers) != 1:
            raise ProtocolLoadError(
                f"{spec} does not export SENDER and RECEIVER, and it has "
                f"{len(senders)} Sender and {len(receivers)} Receiver subclasses so "
                "the harness cannot guess. add `SENDER, RECEIVER = MySender, MyReceiver`"
            )
        sender, receiver = senders[0], receivers[0]

    if not (isinstance(sender, type) and issubclass(sender, Sender)):
        raise ProtocolLoadError(f"{spec}: SENDER must subclass transportlab.api.Sender")
    if not (isinstance(receiver, type) and issubclass(receiver, Receiver)):
        raise ProtocolLoadError(f"{spec}: RECEIVER must subclass transportlab.api.Receiver")
    return sender, receiver


def label(spec: str) -> str:
    """Short, stable name for the results file."""
    try:
        sender, receiver = load(spec)
    except ProtocolLoadError:
        return spec
    return f"{sender.__module__.split('.')[-1]}:{sender.__name__}/{receiver.__name__}"
