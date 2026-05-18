# Required Libraries
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from aif360.datasets import AdultDataset
from aif360.metrics import BinaryLabelDatasetMetric
from backend.events import Event, aemit




async def basicStatistics(data:pd.DataFrame):
    """
    This function returns basic statistics of the dataset
    """
    await aemit(Event('SummaryStatistics', {'summary statistics': data.describe()}))
    await aemit(Event('SummaryStatistics', {'missing_values': data.isnull().sum()}))
    await aemit(Event('SummaryStatistics', {'data_types': data.dtypes}))

    #get distinct values for each column
    distinct_values = {}
    for col in data.columns:
        distinct_values[col] = len(data[col].unique())
    return distinct_values
    