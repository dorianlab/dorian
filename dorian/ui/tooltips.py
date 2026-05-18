"""
dorian/ui/tooltips.py
---------------------
Backend-authoritative tooltip definitions for the Dorian UI.

Each entry is keyed by a *target ID* that matches the ``data-tooltip-id``
attribute on the corresponding frontend element.

Fields
------
title   : str   Short heading shown in the tooltip.
content : str   Full explanatory text.
step    : int   Suggested onboarding order (1 = first).  Components may use
                this to build a step-through tour.  0 means "show at any time".

Step grouping
-------------
Steps are grouped by *view* so the sequential tour never jumps between
screens.  The user transitions from one view to the next exactly once,
at a clearly marked boundary step whose content tells them which button
to click.

  Group A  (steps 1-7)   — Sidebar: session setup (always visible in session view)
  Group B  (steps 8-9)   — Sidebar: pipeline selection (transition point → canvas)
  Group C  (steps 10-13) — Canvas: composition & execution (visible when pipeline loaded)
  Group D  (step 14)     — Wrap-up, always visible
  Group E  (step 15)     — Agent mode (header, always visible — last step)

Adding a new tooltip
--------------------
1. Add an entry here (no frontend code change required).
2. Annotate the matching frontend component/wrapper with::

     data-tooltip-id="your-new-key"

3. The tooltip is delivered to every connected session at init time via
   the ``ui/tooltips`` WebSocket event.

IMPORTANT: Every key in this dict must be unique.  Python silently
overwrites earlier entries when a key is repeated — this breaks the tour.
"""
from __future__ import annotations

TOOLTIPS: dict[str, dict] = {

    # ── Group A: Sidebar — session setup (steps 1-7) ──────────────────────
    # All visible in the session view without needing a pipeline loaded.
    # No view switch required between these steps.

    "dataset-import": {
        "title": "Import a Dataset",
        "content": (
            "Import a dataset from a remote URL or a shared repository. "
            "Dorian fetches and profiles the file automatically. "
            "Use this when your data lives outside your local machine."
        ),
        "step": 1,
    },
    "dataset-upload": {
        "title": "Load a Dataset",
        "content": (
            "Upload a CSV, Excel, JSON, or Parquet file. "
            "Dorian automatically profiles the dataset (column types, distributions, "
            "missing values) and uses the profile to recommend suitable ML pipelines. "
            "Tip: start with a clean CSV for the best experience."
        ),
        "step": 2,
    },
    "loaded-files": {
        "title": "Loaded Files",
        "content": (
            "Files you upload in this session appear here. "
            "Select a file to bind it to the current pipeline — "
            "the dataset is then available to all dorian.io.dataset nodes. "
            "You can toggle visibility (public/private) and remove files "
            "you no longer need."
        ),
        "step": 3,
    },
    "task-selection": {
        "title": "Data Science Task",
        "content": (
            "Choose the type of ML problem you want to solve "
            "(e.g. Classification, Regression, Clustering). "
            "This selection filters pipeline recommendations to only show "
            "relevant algorithms. You can change it at any time."
        ),
        "step": 4,
    },
    "eval-selection": {
        "title": "Evaluation Procedure",
        "content": (
            "Select how pipeline performance should be measured "
            "(e.g. Cross-validation, Train/test split). "
            "The chosen procedure is applied automatically when you run a pipeline."
        ),
        "step": 5,
    },
    "objectives-panel": {
        "title": "Ranking Objectives",
        "content": (
            "Ranking objectives tune the pipeline recommendation engine. "
            "Order matters: the first objective is the primary criterion, "
            "the second is secondary, and so on. Drag to reorder, remove "
            "objectives you don't need, add predefined ones from the search bar, "
            "or create fully custom objectives with your own scoring function. "
            "Changing the list or its order immediately re-ranks recommendations."
        ),
        "step": 6,
    },
    "pipeline-import": {
        "title": "Import a Pipeline",
        "content": (
            "Import a pipeline from a JSON export or a Python script. "
            "JSON files restore the full canvas layout; Python files are "
            "parsed and converted into a visual pipeline automatically."
        ),
        "step": 7,
    },
    # ── Group B: Sidebar — pipeline selection / transition (steps 8-9) ──
    # Still on the sidebar, but these steps trigger the transition to the
    # canvas view.  The tour content tells the user to click a card or the
    # Compose button to continue.

    "recommendation-feed": {
        "title": "Recommended Pipelines",
        "content": (
            "Dorian suggests complete ML pipelines ranked by predicted suitability "
            "for your dataset and task. Recommendations update automatically when "
            "you change your task, dataset, or ranking objectives, and the ranking "
            "engine learns from your interactions in real time — the more you "
            "explore, the better the suggestions become. "
            "Pick a card to load it onto the canvas — the tour will follow you there."
        ),
        "step": 8,
    },
    "compose-pipeline": {
        "title": "Compose a Pipeline",
        "content": (
            "Don't want a recommendation? Click Compose to open a blank canvas "
            "and build your pipeline from scratch. "
            "You can drag operators from the catalog, add preprocessing, modeling, "
            "and evaluation steps, or import a pipeline from a Python file. "
            "Click Compose (or pick a recommendation above) to continue — "
            "the tour will follow you onto the canvas."
        ),
        "step": 9,
    },

    # ── Group C: Canvas — composition & execution (steps 10-13) ─────────
    # All visible once a pipeline is loaded on the canvas.
    # No view switch required between these steps.

    "canvas": {
        "title": "Pipeline Canvas",
        "content": (
            "This is where you build your ML pipeline. "
            "Drag operators from the left sidebar onto the canvas, "
            "then connect nodes by dragging from an output handle (bottom) "
            "to an input handle (top) of another node. "
            "The canvas auto-layouts nodes — double-click empty space to fit all."
        ),
        "step": 10,
    },
    "operator-catalog": {
        "title": "Operator Catalog",
        "content": (
            "Browse and drag operators onto the canvas. "
            "Categories include preprocessing (StandardScaler, OneHotEncoder), "
            "models (RandomForest, SVM, AdaBoost), and utilities (train_test_split). "
            "You can also create custom Snippets — inline Python code blocks — "
            "by dragging the Snippet template."
        ),
        "step": 11,
    },
    "run-pipeline-button": {
        "title": "Run Your Pipeline",
        "content": (
            "Click Run to execute the current pipeline against your loaded dataset. "
            "Each node's status (queued, running, success, failed) is updated in "
            "real time. After execution, inspect outputs by clicking individual nodes. "
            "The debugger may surface risk suggestions — review them in the panel below."
        ),
        "step": 12,
    },
    "version-history": {
        "title": "Version History",
        "content": (
            "Every Run or Save creates a snapshot of your pipeline. "
            "Click the clock icon to browse previous versions and restore any of them."
        ),
        "step": 13,
    },

    # ── Group D: Wrap-up (step 14) ──────────────────────────────────────
    # Always visible.

    "feedback-button": {
        "title": "Report Bugs & Share Feedback",
        "content": (
            "This is the most important button in the alpha! "
            "Click here to report bugs, suggest features, or share any observation. "
            "Every piece of feedback is read by the team — "
            "there is no feedback too small or too obvious. "
            "Tell us what breaks, what's missing, and what would make "
            "Dorian useful in your daily work."
        ),
        "step": 14,
    },

    # ── Group E: Agent mode (step 15 — header, always visible) ──────────
    # Last step of the tour. Agent Mode lives in the top header and is
    # visible from every screen, so the tour ends here regardless of which
    # view the user finishes on.

    "agent-panel": {
        "title": "Agent Mode",
        "content": (
            "Open the Agent Panel to enable Agent Mode. "
            "Agent Mode tags every outbound event with an agentDriven flag "
            "so the backend can distinguish automated actions from manual ones, "
            "and the panel ships a full API reference covering REST endpoints, "
            "WebSocket events, and typical integration workflows. "
            "Flip this on when you want to drive Dorian programmatically."
        ),
        "step": 15,
    },

    # ── Contextual (step 0 — hover only, not part of sequential tour) ───

    "pairwise-comparison": {
        "title": "Compare Pipelines",
        "content": (
            "Compare two pipelines side by side. "
            "Click 'Select this' under the pipeline you prefer, "
            "or 'No preference' if both look equally good. "
            "Your votes improve future recommendations."
        ),
        "step": 0,
    },

    # ── Operator nodes ───────────────────────────────────────────────────

    "operator-node": {
        "title": "Operator Node",
        "content": (
            "An operator wraps a Python callable (e.g. pandas.read_csv, "
            "sklearn.preprocessing.StandardScaler). "
            "Connect its input handles to data sources and its output handles "
            "to downstream operators."
        ),
        "step": 0,
    },
    "parameter-node": {
        "title": "Parameter Node",
        "content": (
            "A parameter provides a constant value to the pipeline "
            "(e.g. a file path, an integer hyperparameter). "
            "Edit the value by clicking on the node."
        ),
        "step": 0,
    },
    "snippet-node": {
        "title": "Custom Snippet",
        "content": (
            "Write arbitrary Python code to transform data between operators. "
            "The function receives upstream outputs as positional arguments "
            "and should return the value(s) passed to downstream nodes."
        ),
        "step": 0,
    },

    # ── AI Debugger ──────────────────────────────────────────────────────

    "debugger": {
        "title": "Pipeline Debugger",
        "content": (
            "The debugger continuously analyzes your pipeline for potential risks "
            "(bias, data leakage, fairness issues) using deterministic, rule-based checks. "
            "Suggestions appear as cards below the canvas. Click 'Accept' to let Dorian "
            "automatically rewrite your pipeline, or 'Dismiss' to ignore the suggestion."
        ),
        "step": 0,
    },

    # ── Suggestion bar (AI Debugger output) ─────────────────────────────

    "suggestion-bar": {
        "title": "Debugger Suggestions",
        "content": (
            "When the debugger detects risks in your pipeline, suggestions "
            "appear here as cards. Each card describes a potential issue "
            "(bias, data leakage, missing preprocessing) and offers a one-click "
            "mitigation that rewrites your pipeline automatically. "
            "You can accept, dismiss, or inspect each suggestion."
        ),
        "step": 0,
    },

    # ── Evaluation panel (after execution) ──────────────────────────────

    "evaluation-panel": {
        "title": "Evaluation Results",
        "content": (
            "After running a pipeline, evaluation metrics appear here. "
            "Compare metrics across pipeline versions to track improvements. "
            "The metrics shown depend on your selected evaluation procedure "
            "and ranking objectives."
        ),
        "step": 0,
    },

    # ── Node output inspector ───────────────────────────────────────────

    "node-output": {
        "title": "Node Output",
        "content": (
            "Click any node after execution to inspect its output. "
            "For data operators you will see a table preview; "
            "for models you will see fitted parameters. "
            "Failed nodes show the error traceback."
        ),
        "step": 0,
    },
}
