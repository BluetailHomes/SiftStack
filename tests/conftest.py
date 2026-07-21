# Manual/live-integration scripts that match pytest's test_*.py naming
# convention but aren't real pytest suites — they run unconditional
# module-level code (sys.exit(), live API calls) and crash collection
# if imported. Run them directly instead, e.g.:
#   python tests/test_e2e_obituary.py
#   python tests/test_entity_researcher.py
#   python tests/test_e2e_family_tree.py
collect_ignore = [
    "test_e2e_obituary.py",
    "test_entity_researcher.py",
    "test_e2e_family_tree.py",
]
