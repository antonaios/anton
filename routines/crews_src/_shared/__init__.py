# Marker so ``_shared.boundary`` imports deterministically under
# ``python -m <crew_module>`` from the crew dir (Python 3.11). Keep this
# package tiny and duplication-friendly — future crews copy
# hello_world_crew.py without dragging a complex shared lib.
