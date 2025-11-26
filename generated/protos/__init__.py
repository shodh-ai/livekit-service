"""Compatibility shim for legacy imports.

This module makes `generated.protos.interaction_pb2` resolve to the
same generated protobuf module as `rox.generated.protos.interaction_pb2`.
"""

from rox.generated.protos import interaction_pb2, interaction_pb2_grpc  # type: ignore

__all__ = ["interaction_pb2", "interaction_pb2_grpc"]
