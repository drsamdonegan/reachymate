"""Gesture labels used by the adapters."""

ACK_GESTURE = "ack"
DENY_GESTURE = "deny"
IDLE_GESTURE = "idle"


def gesture_poses(name: str):
    """Small bounded poses; manager suppresses them during capture and park."""
    from reachy_mini.utils import create_head_pose

    if name == ACK_GESTURE:
        return [create_head_pose(pitch=8), create_head_pose()]
    if name == DENY_GESTURE:
        return [create_head_pose(yaw=-8), create_head_pose(yaw=8), create_head_pose()]
    return [create_head_pose()]
