"""The web app: one read-only UI for runs (live), the lab, datasets and their stage viewers, and pipelines.

``python -m court web`` serves it. It is a consumer like the command line: it
reads runs, datasets, pipelines and experiment records through ``court.core``,
``court.lab`` and ``court.reports``, and changes nothing.
"""
