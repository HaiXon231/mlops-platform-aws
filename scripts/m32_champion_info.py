"""Read the current champion alias without changing Registry metadata."""

import argparse
import json

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", default="http://127.0.0.1:5000")
    parser.add_argument("--model-name", default="nyc-taxi-trip-duration")
    args = parser.parse_args()
    client = MlflowClient(tracking_uri=args.tracking_uri)
    try:
        version = client.get_model_version_by_alias(args.model_name, "champion")
    except MlflowException as error:
        if error.error_code != "INVALID_PARAMETER_VALUE" or "alias champion not found" not in str(error).lower():
            raise
        print(json.dumps({"model_name": args.model_name, "champion_version": None}))
        return
    print(json.dumps({"model_name": args.model_name, "champion_version": version.version,
                      "mlflow_run_id": version.run_id, "validation_status": version.tags.get("validation_status")}))


if __name__ == "__main__":
    main()
