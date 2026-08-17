"""
Enhanced Warning System for Limiter

This module provides a warning system with monitoring periods for users who exceed limits.
It includes:
- UserWarning dataclass for tracking user warnings
- EnhancedWarningSystem class for managing warnings and monitoring
- Helper functions for safe external calls
"""

# Import helper functions
from utils.warning_system.helpers import (
    safe_send_logs,
    safe_send_warning_log,
    safe_send_disable_notification,
    safe_disable_user,
    safe_disable_user_with_punishment,
)

# Import UserLimitWarning dataclass (with UserWarning alias)
from utils.warning_system.user_warning import UserLimitWarning, UserWarning

# Import EnhancedWarningSystem and global instances/accessors
from utils.warning_system.enhanced_system import (
    EnhancedWarningSystem,
    get_warning_system,
    warning_system,
)

__all__ = [
    # Helpers
    "safe_send_logs",
    "safe_send_warning_log",
    "safe_send_disable_notification",
    "safe_disable_user",
    "safe_disable_user_with_punishment",
    # UserWarning / UserLimitWarning
    "UserLimitWarning",
    "UserWarning",
    # EnhancedWarningSystem
    "EnhancedWarningSystem",
    "get_warning_system",
    "warning_system",
]
