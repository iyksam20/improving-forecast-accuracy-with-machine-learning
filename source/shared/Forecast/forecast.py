# #####################################################################################################################
#  Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.                                            #
#                                                                                                                     #
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance     #
#  with the License. A copy of the License is located at                                                              #
#                                                                                                                     #
#  http://www.apache.org/licenses/LICENSE-2.0                                                                         #
#                                                                                                                     #
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES  #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions     #
#  and limitations under the License.                                                                                 #
# #####################################################################################################################

from operator import itemgetter
from os import environ

from shared.Dataset.dataset_file import DatasetFile
from shared.DatasetGroup.dataset_group import DatasetGroup
from shared.Predictor.predictor import Predictor
from shared.helpers import ForecastClient
from shared.logging import get_logger
from shared.status import Status

logger = get_logger(__name__)


class Export:
    """Used to hold the status of an Amazon Forecast forecast export"""

    status = Status.DOES_NOT_EXIST


class Forecast(ForecastClient):
    """Represents the desired state of a forecast generated by Amazon Forecast"""

    def __init__(
        self, predictor: Predictor, dataset_group: DatasetGroup, **forecast_config
    ):
        self._predictor = predictor
        self._dataset_group = dataset_group
        self._forecast_config = forecast_config

        # Use these parameters only for validation.
        self._forecast_params = {
            "ForecastName": "PLACEHOLDER",
            "PredictorArn": f"arn:aws:forecast:us-east-1:"
            + "1" * 12
            + ":predictor/PredictorName",
            **self._forecast_config,
        }
        super().__init__(resource="forecast", **self._forecast_params)

    @property
    def arn(self) -> str:
        """
        Get the ARN of this resource
        :return: The ARN of this resource if it exists, otherwise None
        """
        past_forecasts = self.history()
        if not past_forecasts:
            return None

        return past_forecasts[0].get("ForecastArn")

    def history(self):
        """
        Get this Forecast history from the Amazon Forecast Service.
        :return: List of past forecasts, in descending order by creation time
        """
        past_forecasts = []
        filters = [
            {
                "Key": "DatasetGroupArn",
                "Condition": "IS",
                "Value": self._dataset_group.arn,
            }
        ]

        paginator = self.cli.get_paginator("list_forecasts")
        iterator = paginator.paginate(Filters=filters)
        for page in iterator:
            past_forecasts.extend(page.get("Forecasts", []))

        past_forecasts = sorted(
            past_forecasts, key=itemgetter("LastModificationTime"), reverse=True
        )
        return past_forecasts

    @property
    def status(self) -> Status:
        """
        Get the status of this forecast as defined. The status might be DOES_NOT_EXSIST if a forecast of the desired
        format does not yet exist.
        :return: Status
        """
        past_forecasts = self.history()

        # check if a forecast has been created:
        if not past_forecasts:
            logger.debug("No past forecasts found")
            return Status.DOES_NOT_EXIST

        past_status = self.cli.describe_forecast(
            ForecastArn=past_forecasts[0].get("ForecastArn")
        )

        # if the past forecast was generated with a different predictor, regenerate
        if past_status.get("PredictorArn") != self._predictor.arn:
            logger.debug(
                "Most recent forecast was generated with a different predictor, a new forecast should be created"
            )
            return Status.DOES_NOT_EXIST

        # if the datasets in the datasetgroup have changed after the previous forecast
        # was generated, regenerate the forecast.
        for dataset in self._dataset_group.datasets:
            if dataset.get("LastModificationTime") > past_status.get("CreationTime"):
                logger.debug(
                    "Datasets have changed since last forecast generation, a new forecast should be created "
                )
                return Status.DOES_NOT_EXIST

        return Status[past_status.get("Status")]

    @property
    def _latest_timestamp(self):
        """
        Forecasts can use existing predictors with new data. Use the dataset latest timestamp as the forecast timestamp
        :return:
        """
        return self._dataset_group.latest_timestamp

    def create(self):
        """
        Create the forecast
        :return: None
        """
        forecast_name = f"forecast_{self._dataset_group.dataset_group_name}_{self._latest_timestamp}"

        try:
            logger.info("Creating forecast %s" % forecast_name)
            self.cli.create_forecast(
                ForecastName=forecast_name,
                PredictorArn=self._predictor.arn,
                Tags=self.tags,
                **self._forecast_config,
            )
        except self.cli.exceptions.ResourceAlreadyExistsException:
            logger.debug("Forecast %s is already creating" % forecast_name)
        except self.cli.exceptions.ResourceInUseException as excinfo:
            logger.debug("Forecast %s is updating: %s" % (forecast_name, str(excinfo)))

    def export_history(self, status="ACTIVE"):
        """
        Get this Predictor history from the Amazon Forecast service.
        :param status: The Status of the predictor(s) to return
        :return: List of past predictors, in descending order by creation time
        """
        past_exports = []
        filters = [
            {
                "Condition": "IS",
                "Key": "ForecastArn",
                "Value": self.arn,
            },
            {"Condition": "IS", "Key": "Status", "Value": status},
        ]

        paginator = self.cli.get_paginator("list_forecast_export_jobs")
        iterator = paginator.paginate(Filters=filters)
        for page in iterator:
            past_exports.extend(page.get("ForecastExportJobs", []))

        past_exports = sorted(
            past_exports, key=itemgetter("CreationTime"), reverse=True
        )
        logger.debug("there are {%d} exports: %s" % (len(past_exports), past_exports))

        return past_exports

    def export(self, dataset_file: DatasetFile) -> Export:
        """
        Export/ check on an export of this Forecast
        :param dataset_file: The dataset file last updated that generated this export
        :return: Status
        """
        if not self.arn:
            raise ValueError("Forecast does not yet exist - cannot perform export.")

        export_name = (
            f"export_{self._dataset_group.dataset_group_name}_{self._latest_timestamp}"
        )

        past_export = Export()
        try:
            past_status = self.cli.describe_forecast_export_job(
                ForecastExportJobArn=self.arn.replace(
                    ":forecast/", ":forecast-export-job/"
                )
                + f"/{export_name}"
            )
            past_export.status = Status[past_status.get("Status")]
        except self.cli.exceptions.ResourceInUseException as excinfo:
            logger.debug(
                "Forecast export %s is updating: %s" % (export_name, str(excinfo))
            )
        except self.cli.exceptions.ResourceNotFoundException:
            logger.info("Creating forecast export %s" % export_name)
            self.cli.create_forecast_export_job(
                ForecastArn=self.arn,
                ForecastExportJobName=export_name,
                Destination={
                    "S3Config": {
                        "Path": f"s3://{dataset_file.bucket}/exports/{export_name}",
                        "RoleArn": environ.get("FORECAST_ROLE"),
                    }
                },
            )
            past_export.status = Status.CREATE_PENDING

        logger.debug(
            "Export status for %s is %s" % (export_name, str(past_export.status))
        )
        return past_export
