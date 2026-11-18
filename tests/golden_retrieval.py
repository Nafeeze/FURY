"""Versioned golden set for runbook retrieval: (query, expected runbook id). Add cases when a retrieval miss is found;
never delete a failing case to make a metric pass. Queries phrased the way engineers actually talk."""
GOLDEN_VERSION = "1.0"
GOLDEN = [
    ("out of disk space on F", "AWS-DISK-001"), ("drive F is almost full", "AWS-DISK-001"), ("no space left on device", "AWS-DISK-001"),
    ("how do I clear temp files safely", "AWS-DISK-001"), ("volume almost full", "AWS-DISK-001"),
    ("the website is down", "AWS-SVC-001"), ("IIS not responding", "AWS-SVC-001"), ("W3SVC stopped", "AWS-SVC-001"),
    ("web service not running", "AWS-SVC-001"), ("service stopped after reboot", "AWS-SVC-001"),
    ("CPU is pegged", "AWS-CPU-001"), ("server is very slow", "AWS-CPU-001"), ("runaway process eating cpu", "AWS-CPU-001"),
    ("high cpu utilization", "AWS-CPU-001"), ("kill the rogue process", "AWS-CPU-001"),
    ("escalate to level 2", "INC-ESC-001"), ("L1 to L2 handover", "INC-ESC-001"),
]
# Queries with NO supporting runbook: retrieval must return nothing rather than a confident wrong citation.
UNSUPPORTED = ["memory leak", "how to bake a cake", "database replication lag", "ssl certificate expired"]
