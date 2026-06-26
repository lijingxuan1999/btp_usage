"""
Stub implementation of sap_cloud_sdk.agent_decorators.

The real package (sap-cloud-sdk) pulls in protovalidate -> cel-python -> google-re2,
which the platform sfw firewall blocks unconditionally.  The decorators below are
pass-through no-ops: they preserve the decorated function unchanged, which is all
the BTP Usage Agent requires at runtime.
"""
from typing import Any, Callable, Optional, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def agent_model(
    key: Optional[str] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
) -> Callable[[F], F]:
    """Mark a function as the model-name provider for this agent (no-op stub)."""
    def decorator(func: F) -> F:
        return func
    return decorator


def agent_config(
    key: Optional[str] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
    validation: Optional[dict] = None,
) -> Callable[[F], F]:
    """Mark a function as an agent config value provider (no-op stub)."""
    def decorator(func: F) -> F:
        return func
    return decorator


def prompt_section(
    key: Optional[str] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
    validation: Optional[dict] = None,
) -> Callable[[F], F]:
    """Mark a function as a prompt-section provider (no-op stub)."""
    def decorator(func: F) -> F:
        return func
    return decorator
