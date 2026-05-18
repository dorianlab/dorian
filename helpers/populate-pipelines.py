import logging
from pathlib import Path
import pandas as pd
import openml
import re
import sys
# from dorian.collection.parser import parse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Constants
CURRENT_DIR = Path.cwd()
FLOWS_PATH = CURRENT_DIR / "flows.csv"
CHECKED_PATH = CURRENT_DIR / "checked_pipelines.csv"


def make_pipeline(fid: int) -> str:
    """Generate a pipeline string from an OpenML flow ID."""
    try:
        flow = openml.flows.get_flow(fid)
        # Extract and format parameters
        params = [
            (k, v["value"] if isinstance(v, dict) else v)
            for k, v in flow.parameters.items()
            if v and k not in ["steps", "estimators", "memory", "verbose"]
        ]
        params_str = ", ".join(f"{k}={v}" for k, v in params) if params else ""
        # Construct the pipeline string
        pipeline_str = flow.name + (
            f"({params_str})" if (params_str or flow.name[-1] != ")") else ""
        )
        return pipeline_str
    except Exception as e:
        logger.error(f"Error generating pipeline for flow {fid}: {e}")
        raise


def load_or_fetch_flows(fpath: str) -> pd.DataFrame:
    """Load flows from a CSV file or fetch them from OpenML if the file doesn't exist."""
    try:
        if Path(fpath).exists():
            logger.info(f"Loading flows from existing file: {fpath}")
            return pd.read_csv(fpath)
        else:
            logger.info(f"File not found at {fpath}. Fetching flows from OpenML.")
            flows = openml.flows.list_flows(output_format="dataframe")
            flows = flows[flows.name.str.contains("sklearn") & (flows["version"].astype(int) == 1)][:1]
            logger.info(f"Fetched {len(flows)} flows from OpenML.")
            # Generate pipeline strings for each flow
            flows["pipeline"] = flows.id.apply(make_pipeline)
            # Sort flows by pipeline string length
            flows = flows.reindex(flows.pipeline.str.len().sort_values().index)
            # Save flows to CSV for future use
            flows.to_csv(fpath, index=False)
            logger.info(f"Saved flows to {fpath}.")
            return flows
    except Exception as e:
        logger.error(f"Error loading or fetching flows: {e}")
        raise


def load_checked_pipelines(checked_path: str) -> list:
    """Load the list of already checked pipeline IDs from a CSV file."""
    try:
        if Path(checked_path).exists():
            logger.info(f"Loading checked pipelines from {checked_path}.")
            return pd.read_csv(checked_path, names=["fid", "tag"])["fid"].to_list()
        else:
            logger.info(f"No checked pipelines file found at {checked_path}.")
            return []
    except Exception as e:
        logger.error(f"Error loading checked pipelines: {e}")
        raise

"""
    def validate_pipeline(fid: int, code: str, checked_path: str) -> bool:
        try:
            # Skip pipelines with specific patterns
            if re.findall(r"TEST|C37|C0x", code):
                logger.warning(f"Skipping pipeline {fid} due to invalid pattern: {code}")
                with open(checked_path, "a") as f:
                    f.write(f"{fid},s\n")
                return False

            logger.info(f"Validating pipeline {fid}: {code}")

            # dag = parse(code, language=SupportedLanguage.python)
            # logger.info(f"Parsed DAG: {dag}")

            while True:
                response = input("[C]orrect, [W]rong, [S]kip, [E]dit, [Q]uit? ").lower()
                match response:
                    case "q":
                        logger.info("User chose to quit.")
                        return True  # Quit
                    case "c" | "w" | "s" | "e":
                        with open(checked_path, "a") as f:
                            f.write(f"{fid},{response}\n")
                        logger.info(f"Recorded response '{response}' for pipeline {fid}.")
                        return False  # Continue
                    case _:
                        logger.warning(f"Invalid tag: {response}")
        except Exception as e:
            logger.error(f"Error validating pipeline {fid}: {e}")
            raise

"""


def main():
    """Main function to orchestrate the pipeline validation workflow."""
    try:
        logger.info("Starting pipeline validation workflow.")
        # Load or fetch flows
        flows = load_or_fetch_flows(FLOWS_PATH)
        logger.info(f"Total flows loaded: {len(flows)}")
        # Filter out already checked pipelines
        done_fids = load_checked_pipelines(CHECKED_PATH)
        flows = flows[~flows.id.isin(done_fids)]
        logger.info(f"Pipelines to validate: {len(flows)}")
        # Validate pipelines
        for fid, code in flows.loc[:, ["id", "pipeline"]].itertuples(index=False):
            code = make_pipeline(fid)
            logger.info(f"{code}")
            # if validate_pipeline(fid, code, CHECKED_PATH):
            #     logger.info("Validation workflow terminated by user.")
            #     break  # Quit if the user chooses to

        logger.info("Pipeline validation workflow completed.")
    except Exception as e:
        logger.error(f"Error in main workflow: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()