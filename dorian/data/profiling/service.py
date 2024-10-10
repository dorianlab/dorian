from collections import deque
from dataclasses import dataclass
from typing import List
import inspect

from metafeatures import *


@dataclass
class MetaFeature:
    name: str
    X_req: bool = False
    Y_req: bool = False
    ftType_req: bool = False
    dependency: List[str] = []


metaFeaturesFunctions_NoArgs=[
    MetaFeature("LogNumberOfInstances", dependency=["NumberOfInstances"]),
    MetaFeature("NumberOfInstancesWithMissingValues", dependency=["MissingValues"]),
    MetaFeature("NumberOfFeaturesWithMissingValues", dependency=["MissingValues"]),
    MetaFeature("PercentageOfInstancesWithMissingValues", dependency=["NumberOfInstancesWithMissingValues","NumberOfInstances"]),
    MetaFeature("PercentageOfFeaturesWithMissingValues", dependency=["NumberOfFeaturesWithMissingValues","NumberOfFeatures"]),
    MetaFeature("RatioNumericalToNominal", dependency=["NumberOfCategoricalFeatures","NumberOfNumericFeatures"]),
    MetaFeature("RatioNominalToNumerical", dependency=["NumberOfCategoricalFeatures","NumberOfNumericFeatures"]),
    MetaFeature("DatasetRatio", dependency=["NumberOfFeatures","NumberOfInstances"]),
    MetaFeature("LogDatasetRatio", dependency=["DatasetRatio"]),
    MetaFeature("InverseDatasetRatio", dependency=["NumberOfInstances","NumberOfFeatures"]),
    MetaFeature("LogInverseDatasetRatio", dependency=["InverseDatasetRatio"]),
    MetaFeature("SymbolsMin", dependency=["NumSymbols"]),
    MetaFeature("SymbolsMax", dependency=["NumSymbols"]),
    MetaFeature("SymbolsMean", dependency=["NumSymbols"]),
    MetaFeature("SymbolsSTD", dependency=["NumSymbols"]),
    MetaFeature("SymbolsSum", dependency=["NumSymbols"]),
    MetaFeature("KurtosisMin", dependency=["Kurtosisses"]),
    MetaFeature("KurtosisMax", dependency=["Kurtosisses"]),
    MetaFeature("KurtosisMean", dependency=["Kurtosisses"]),
    MetaFeature("KurtosisSTD", dependency=["Kurtosisses"]),
    MetaFeature("SkewnessMin", dependency=["Skewnesses"]),
    MetaFeature("SkewnessMax", dependency=["Skewnesses"]),
    MetaFeature("SkewnessMean", dependency=["Skewnesses"]),
    MetaFeature("SkewnessSTD", dependency=["Skewnesses"]),
]

metaFeaturesFunctions_X=[
    MetaFeature("NumberOfInstances"),
    MetaFeature("MissingValues"),
    MetaFeature("NumberOfFeatures"),
    MetaFeature("NumberOfMissingValues", dependency=["MissingValues"]),
    MetaFeature("PercentageOfMissingValues", dependency=["NumberOfMissingValues"]),
    MetaFeature("PCA"),
    MetaFeature("PCAFractionOfComponentsFor95PercentVariance", dependency=["PCA"]),
    MetaFeature("PCAKurtosisFirstPC", dependency=["PCA"]),
    MetaFeature("PCASkewnessFirstPC", dependency=["PCA"]),
]

metaFeaturesFunctions_X_Y=[
    MetaFeature("ClassProbabilityMin", dependency=["ClassOccurences"]),
    MetaFeature("ClassProbabilityMax", dependency=["ClassOccurences"]),
    MetaFeature("ClassProbabilityMean", dependency=["ClassOccurences"]),
    MetaFeature("ClassProbabilitySTD", dependency=["ClassOccurences"]),
    MetaFeature("ClassEntropy"),
    MetaFeature("LandmarkLDA"),
    MetaFeature("LandmarkNaiveBayes"),
    MetaFeature("LandmarkDecisionTree"),
    MetaFeature("LandmarkDecisionNodeLearner"),
    MetaFeature("Landmark1NN"),
]

metaFeaturesFunctions_X_Y_featType=[
    MetaFeature("NumberOfNumericFeatures"),
    MetaFeature("NumberOfCategoricalFeatures"),
    MetaFeature("ClassOccurences"),
    MetaFeature("NumSymbols"),
    MetaFeature("Kurtosisses"),
    MetaFeature("Skewnesses")
]
    

def hasAllRequiredArgsForMF(metafeature:MetaFeature,y,feat_type):
    class_ = globals()[metafeature.name]
    instance=class_()
    fnSignature = inspect.signature(instance)
    reqParameters = [name.lower().strip() for name in fnSignature.parameters.keys()]
    if "x" in reqParameters: metafeature.X_req=True
    if "y" in reqParameters: metafeature.Y_req=True
    if "feat_type" in reqParameters: metafeature.ftType_req=True

    if metafeature.Y_req and y is None:
        return False
    if metafeature.ftType_req and feat_type is None:
        return False
    return True


def calculate_all_metafeatures(
    X,
    y=None,
    feat_type=None
):
    extract_values = lambda dependencies: [metaFeatureValues[key] for key in dependencies if key in metaFeatureValues] if dependencies is not None else []
    metaFeatureValues=dict()
    dependentMetaFeatures=dict()
    metaFeatures = metaFeaturesFunctions_NoArgs+metaFeaturesFunctions_X+metaFeaturesFunctions_X_Y+metaFeaturesFunctions_X_Y_featType

    for metafeature in metaFeatures:
        if not hasAllRequiredArgsForMF(metafeature,y,feat_type):continue 
        _mfdependencies = metafeature.dependency
        _dependenciesResolved = all(dependency in metaFeatureValues for dependency in metafeature.dependency) if metafeature.dependency is not None and len(metafeature.dependency)>0 else True
        if not _dependenciesResolved:
            for _dp in _mfdependencies:
                if _dp in dependentMetaFeatures: dependentMetaFeatures[_dp].append(metafeature)
                else: dependentMetaFeatures[_dp]=[metafeature]
            continue
        _mfdependencyVal = extract_values(_mfdependencies)
        value = submitTaskBasedOnArgs(metafeature,[X,y,feat_type,*_mfdependencyVal])

        metaFeatureValues[metafeature.name]=value
        _calculatedFeatures = deque()
        _calculatedFeatures.extend([metafeature])
        while(len(_calculatedFeatures)>0):
            _calculatedMF=_calculatedFeatures.pop().name
            for _dependent_mf in dependentMetaFeatures.get(_calculatedMF, []):
                _mfdependencies = _dependent_mf.dependency
                _dependenciesResolved = all(dependency in metaFeatureValues for dependency in _dependent_mf.dependency) if _dependent_mf.dependency is not None and len(_dependent_mf.dependency)>0 else True
                if not _dependenciesResolved:continue
                _mfdependencyVal = extract_values(_mfdependencies)
                value = submitTaskBasedOnArgs(_dependent_mf,[X,y,feat_type,*_mfdependencyVal])
                metaFeatureValues[_dependent_mf.name]=value
                _calculatedFeatures.appendleft(_dependent_mf)
    return metaFeatureValues

if __name__ == '__main__':
    # workers and threads 1 for debugging purpose
    cluster = LocalCluster(n_workers=1, threads_per_worker=1, memory_limit='2GB')
    client = Client(cluster)
    print(client)

    X = [[1, 2], [3, 4],[5, 6],[7, 8],[9, 10],[11, 12]]  # Example feature data
    y = [0, 1, 0, 1, 1, 0]            # Example target data
    feat_type = {'feat1': 'numerical', 'feat2': 'numerical','feat3': 'numerical','feat4': 'numerical','feat5': 'numerical','feat6': 'numerical'}  # Feature types
    X_df = pd.DataFrame(X, columns=['feat1', 'feat2'])
    y_np = np.array(y)

    mf_ = calculate_all_metafeatures(X_df, y_np, feat_type)
    for key in mf_:
        print(key+":")
        print(client.gather(mf_[key]))
