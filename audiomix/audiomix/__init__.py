"""audiomix：多采样率剪辑时间轴、真实混音档案与有声导览编排。"""
from .assets import AssetRegistry
from .timeline import TimelineEditor, Clip, ValidationError
from .engine import (
    render_reference, render_block, render_full_vectorized,
    validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
    render_sample_range,
)
from .archive import ArchiveStore, block_key_of
from .packets import (
    Packet, PacketStore, ProductLibrary, Segment, SegmentError,
    packet_identity,
)
from .guide import (
    PlayWindow, TransmitWindow, Hall, RouteMap, DeviceProfile,
    CompiledGuide, compile_guide, Planner, PlanResult, Action,
    simulate_path, summarize_all_routes, compare_profiles,
    verify_plan_symbolic,
)

__all__ = [
    "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
    "render_reference", "render_block", "render_full_vectorized",
    "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
    "render_sample_range",
    "ArchiveStore", "block_key_of",
    "Packet", "PacketStore", "ProductLibrary", "Segment", "SegmentError",
    "packet_identity",
    "PlayWindow", "TransmitWindow", "Hall", "RouteMap", "DeviceProfile",
    "CompiledGuide", "compile_guide", "Planner", "PlanResult", "Action",
    "simulate_path", "summarize_all_routes", "compare_profiles",
    "verify_plan_symbolic",
]
