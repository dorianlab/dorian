import scipy
import numpy as np
import pandas as pd
from collections import defaultdict
from sklearn.multiclass import OneVsRestClassifier

class NumberOfInstances():
    def __call__(self, X):
        return float(X.shape[0])
    
class NumberOfFeatures():
    def __call__(self, X):
        return float(X.shape[1])

class LogNumberOfInstances():
    def __call__(self,NumberOfInstances):
        return np.log(NumberOfInstances)
    
class MissingValues():
    def __call__(self, X):
        missing = pd.isna(X)
        return missing
    
class NumberOfInstancesWithMissingValues():
    def __call__(self,MissingValues):
        num_missing = MissingValues.sum(axis=1)
        return float(np.sum([1 if num > 0 else 0 for num in num_missing]))
    
class NumberOfFeaturesWithMissingValues():
    def __call__(self,MissingValues):
        missing = MissingValues
        num_missing = missing.sum(axis=0)
        return float(np.sum([1 if num > 0 else 0 for num in num_missing]))
    
class PercentageOfInstancesWithMissingValues():
    def __call__(self,NumberOfInstancesWithMissingValues,NumberOfInstances):
        n_missing = NumberOfInstancesWithMissingValues
        n_total = NumberOfInstances
        return float(n_missing / n_total)
    
class PercentageOfFeaturesWithMissingValues():
    def __call__(self, NumberOfFeaturesWithMissingValues,NumberOfFeatures):
        n_missing = NumberOfFeaturesWithMissingValues
        n_total = NumberOfFeatures
        return float(n_missing / n_total)

class NumberOfMissingValues():
    def __call__(self, X, MissingValues):
        if scipy.sparse.issparse(X):
            return float(MissingValues.sum())
        else:
            return float(np.count_nonzero(MissingValues))
    
class PercentageOfMissingValues():
    def __call__(self, X, NumberOfMissingValues):
        return float(NumberOfMissingValues) / float(
            X.shape[0] * X.shape[1]
        )

class NumberOfNumericFeatures():
    def __call__(self, feat_type):
        return np.sum([value == "numerical" for value in feat_type.values()])
    
class NumberOfCategoricalFeatures():
    def __call__(self, feat_type):
        return np.sum([value == "categorical" for value in feat_type.values()])
    
class RatioNumericalToNominal():
    def __call__(self, NumberOfCategoricalFeatures,NumberOfNumericFeatures):
        num_categorical = float(NumberOfCategoricalFeatures)
        num_numerical = float(NumberOfNumericFeatures)
        if num_categorical == 0.0:
            return 0.0
        return num_numerical / num_categorical
    
class RatioNominalToNumerical():
    def __call__(self, NumberOfCategoricalFeatures,NumberOfNumericFeatures):
        num_categorical = float(NumberOfCategoricalFeatures)
        num_numerical = float(NumberOfNumericFeatures)
        if num_numerical == 0.0:
            return 0.0
        else:
            return num_categorical / num_numerical
        
class DatasetRatio():
    def __call__(self,NumberOfFeatures,NumberOfInstances):
        return float(NumberOfFeatures) / float(NumberOfInstances)

class LogDatasetRatio():
    def __call__(self,DatasetRatio):
        return np.log(DatasetRatio)
    
class InverseDatasetRatio():
    def __call__(self,NumberOfInstances,NumberOfFeatures):
        return float(NumberOfInstances) / float(NumberOfFeatures)
    
class LogInverseDatasetRatio():
    def __call__(self,InverseDatasetRatio):
        return np.log(InverseDatasetRatio)
    
class ClassOccurences():
    def __call__(self, X, y, feat_type):
        if len(y.shape) == 2:
            occurences = []
            for i in range(y.shape[1]):
                occurences.append(self(X, y[:, i], feat_type))
            return occurences
        else:
            occurence_dict = defaultdict(float)
            for value in y:
                occurence_dict[value] += 1
            return occurence_dict
        
class ClassProbabilityMin():
    def __call__(self,y,ClassOccurences):
        occurences = ClassOccurences

        min_value = np.iinfo(np.int64).max
        if len(y.shape) == 2:
            for i in range(y.shape[1]):
                for num_occurences in occurences[i].values():
                    if num_occurences < min_value:
                        min_value = num_occurences
        else:
            for num_occurences in occurences.values():
                if num_occurences < min_value:
                    min_value = num_occurences
        return float(min_value) / float(y.shape[0])
    
class ClassProbabilityMax():
    def __call__(self, y,ClassOccurences):
        occurences = ClassOccurences
        max_value = -1

        if len(y.shape) == 2:
            for i in range(y.shape[1]):
                for num_occurences in occurences[i].values():
                    if num_occurences > max_value:
                        max_value = num_occurences
        else:
            for num_occurences in occurences.values():
                if num_occurences > max_value:
                    max_value = num_occurences
        return float(max_value) / float(y.shape[0])
    
class ClassProbabilityMean():
    def __call__(self, y,ClassOccurences):
        occurence_dict = ClassOccurences

        if len(y.shape) == 2:
            occurences = []
            for i in range(y.shape[1]):
                occurences.extend(
                    [occurrence for occurrence in occurence_dict[i].values()]
                )
            occurences = np.array(occurences)
        else:
            occurences = np.array(
                [occurrence for occurrence in occurence_dict.values()], dtype=np.float64
            )
        return (occurences / y.shape[0]).mean()
    
class ClassProbabilitySTD():
    def __call__(self, y,ClassOccurences):
        occurence_dict = ClassOccurences

        if len(y.shape) == 2:
            stds = []
            for i in range(y.shape[1]):
                std = np.array(
                    [occurrence for occurrence in occurence_dict[i].values()],
                    dtype=np.float64,
                )
                std = (std / y.shape[0]).std()
                stds.append(std)
            return np.mean(stds)
        else:
            occurences = np.array(
                [occurrence for occurrence in occurence_dict.values()], dtype=np.float64
            )
            return (occurences / y.shape[0]).std()
        
class NumSymbols():
    def __call__(self, X, feat_type):
        categorical = {
            key: True if value.lower() == "categorical" else False
            for key, value in feat_type.items()
        }
        symbols_per_column = []
        for i in range(X.shape[1]):
            if categorical[X.columns[i] if hasattr(X, "columns") else i]:
                column = X.iloc[:, i] if hasattr(X, "iloc") else X[:, i]
                unique_values = (
                    column.unique() if hasattr(column, "unique") else np.unique(column)
                )
                num_unique = np.sum(pd.notna(unique_values))
                symbols_per_column.append(num_unique)
        return symbols_per_column
    
class SymbolsMin():
    def __call__(self, NumSymbols):
        # The minimum can only be zero if there are no nominal features,
        # otherwise it is at least one
        # TODO: shouldn't this rather be two?
        minimum = None
        for unique in NumSymbols:
            if unique > 0 and (minimum is None or unique < minimum):
                minimum = unique
        return minimum if minimum is not None else 0
    
class SymbolsMax():
    def __call__(self,NumSymbols):
        values = NumSymbols
        if len(values) == 0:
            return 0
        return max(max(values), 0)
    
class SymbolsMean():
    def __call__(self,NumSymbols):
        # TODO: categorical attributes without a symbol don't count towards this
        # measure
        values = [val for val in NumSymbols if val > 0]
        mean = np.nanmean(values)
        return mean if np.isfinite(mean) else 0
    
class SymbolsSTD():
    def __call__(self,NumSymbols):
        values = [val for val in NumSymbols if val > 0]
        std = np.nanstd(values)
        return std if np.isfinite(std) else 0
    
class SymbolsSum():
    def __call__(self,NumSymbols):
        sum = np.nansum(NumSymbols)
        return sum if np.isfinite(sum) else 0
    
class Kurtosisses():
    def __call__(self, X, feat_type):
        numerical = {
            key: True if value.lower() == "numerical" else False
            for key, value in feat_type.items()
        }
        kurts = []
        for i in range(X.shape[1]):
            if numerical[X.columns[i] if hasattr(X, "columns") else i]:
                if np.isclose(
                    np.var(X.iloc[:, i] if hasattr(X, "iloc") else X[:, i]), 0
                ):
                    kurts.append(0)
                else:
                    kurts.append(
                        scipy.stats.kurtosis(
                            X.iloc[:, i] if hasattr(X, "iloc") else X[:, i]
                        )
                    )
        return kurts
    
class KurtosisMin():
    def __call__(self, Kurtosisses):
        kurts = Kurtosisses
        minimum = np.nanmin(kurts) if len(kurts) > 0 else 0
        return minimum if np.isfinite(minimum) else 0
    
class KurtosisMax():
    def __call__(self, Kurtosisses):
        kurts = Kurtosisses
        maximum = np.nanmax(kurts) if len(kurts) > 0 else 0
        return maximum if np.isfinite(maximum) else 0


class KurtosisMean():
    def __call__(self, Kurtosisses):
        kurts = Kurtosisses
        mean = np.nanmean(kurts) if len(kurts) > 0 else 0
        return mean if np.isfinite(mean) else 0


class KurtosisSTD():
    def __call__(self, Kurtosisses):
        kurts = Kurtosisses
        std = np.nanstd(kurts) if len(kurts) > 0 else 0
        return std if np.isfinite(std) else 0
    
class Skewnesses():
    def __call__(self, X, feat_type):
        numerical = {
            key: True if value.lower() == "numerical" else False
            for key, value in feat_type.items()
        }
        skews = []
        for i in range(X.shape[1]):
            if numerical[X.columns[i] if hasattr(X, "columns") else i]:
                if np.isclose(
                    np.var(X.iloc[:, i] if hasattr(X, "iloc") else X[:, i]), 0
                ):
                    skews.append(0)
                else:
                    skews.append(
                        scipy.stats.skew(
                            X.iloc[:, i] if hasattr(X, "iloc") else X[:, i]
                        )
                    )
        return skews
    
class SkewnessMin():
    def __call__(self, Skewnesses):
        skews = Skewnesses
        minimum = np.nanmin(skews) if len(skews) > 0 else 0
        return minimum if np.isfinite(minimum) else 0


class SkewnessMax():
    def __call__(self, Skewnesses):
        skews = Skewnesses
        maximum = np.nanmax(skews) if len(skews) > 0 else 0
        return maximum if np.isfinite(maximum) else 0


class SkewnessMean():
    def __call__(self, Skewnesses):
        skews = Skewnesses
        mean = np.nanmean(skews) if len(skews) > 0 else 0
        return mean if np.isfinite(mean) else 0


class SkewnessSTD():
    def __call__(self, Skewnesses):
        skews = Skewnesses
        std = np.nanstd(skews) if len(skews) > 0 else 0
        return std if np.isfinite(std) else 0
    
class ClassEntropy():
    def __call__(self, y):
        labels = 1 if len(y.shape) == 1 else y.shape[1]

        entropies = []
        for i in range(labels):
            occurence_dict = defaultdict(float)
            for value in y if labels == 1 else y[:, i]:
                occurence_dict[value] += 1
            entropies.append(
                scipy.stats.entropy(
                    [occurence_dict[key] for key in occurence_dict], base=2
                )
            )

        return np.mean(entropies)
    
class LandmarkLDA():
    def __call__(self, X, y):
        import sklearn.discriminant_analysis

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)

        accuracy = 0.0
        try:
            for train, test in kf.split(X, y):
                lda = sklearn.discriminant_analysis.LinearDiscriminantAnalysis()

                if len(y.shape) == 1 or y.shape[1] == 1:
                    lda.fit(
                        X.iloc[train] if hasattr(X, "iloc") else X[train],
                        y.iloc[train] if hasattr(y, "iloc") else y[train],
                    )
                else:
                    lda = OneVsRestClassifier(lda)
                    lda.fit(
                        X.iloc[train] if hasattr(X, "iloc") else X[train],
                        y.iloc[train] if hasattr(y, "iloc") else y[train],
                    )

                predictions = lda.predict(
                    X.iloc[test] if hasattr(X, "iloc") else X[test],
                )
                accuracy += sklearn.metrics.accuracy_score(
                    predictions,
                    y.iloc[test] if hasattr(y, "iloc") else y[test],
                )
            return accuracy / 5
        except scipy.linalg.LinAlgError as e:
            print("LDA failed: %s Returned 0 instead!" % e)
            return np.NaN
        except ValueError as e:
            print("LDA failed: %s Returned 0 instead!" % e)
            return np.NaN
        
class LandmarkNaiveBayes():
    def __call__(self, X, y):
        import sklearn.naive_bayes

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)

        accuracy = 0.0
        for train, test in kf.split(X, y):
            nb = sklearn.naive_bayes.GaussianNB()

            if len(y.shape) == 1 or y.shape[1] == 1:
                nb.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            else:
                nb = OneVsRestClassifier(nb)
                nb.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )

            predictions = nb.predict(
                X.iloc[test] if hasattr(X, "iloc") else X[test],
            )
            accuracy += sklearn.metrics.accuracy_score(
                predictions,
                y.iloc[test] if hasattr(y, "iloc") else y[test],
            )
        return accuracy / 5
    
class LandmarkDecisionTree():
    def __call__(self, X, y):
        import sklearn.tree

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)

        accuracy = 0.0
        for train, test in kf.split(X, y):
            random_state = sklearn.utils.check_random_state(42)
            tree = sklearn.tree.DecisionTreeClassifier(random_state=random_state)

            if len(y.shape) == 1 or y.shape[1] == 1:
                tree.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            else:
                tree = OneVsRestClassifier(tree)
                tree.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )

            predictions = tree.predict(
                X.iloc[test] if hasattr(X, "iloc") else X[test],
            )
            accuracy += sklearn.metrics.accuracy_score(
                predictions,
                y.iloc[test] if hasattr(y, "iloc") else y[test],
            )
        return accuracy / 5
    
class LandmarkDecisionNodeLearner():
    def __call__(self, X, y):
        import sklearn.tree

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)

        accuracy = 0.0
        for train, test in kf.split(X, y):
            random_state = sklearn.utils.check_random_state(42)
            node = sklearn.tree.DecisionTreeClassifier(
                criterion="entropy",
                max_depth=1,
                random_state=random_state,
                min_samples_split=2,
                min_samples_leaf=1,
                max_features=None,
            )
            if len(y.shape) == 1 or y.shape[1] == 1:
                node.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            else:
                node = OneVsRestClassifier(node)
                node.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            predictions = node.predict(
                X.iloc[test] if hasattr(X, "iloc") else X[test],
            )
            accuracy += sklearn.metrics.accuracy_score(
                predictions,
                y.iloc[test] if hasattr(y, "iloc") else y[test],
            )
        return accuracy / 5
    
class LandmarkRandomNodeLearner():
    def __call__(self, X, y):
        import sklearn.tree

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)
        accuracy = 0.0

        for train, test in kf.split(X, y):
            random_state = sklearn.utils.check_random_state(42)
            node = sklearn.tree.DecisionTreeClassifier(
                criterion="entropy",
                max_depth=1,
                random_state=random_state,
                min_samples_split=2,
                min_samples_leaf=1,
                max_features=1,
            )
            node.fit(
                X.iloc[train] if hasattr(X, "iloc") else X[train],
                y.iloc[train] if hasattr(y, "iloc") else y[train],
            )
            predictions = node.predict(
                X.iloc[test] if hasattr(X, "iloc") else X[test],
            )
            accuracy += sklearn.metrics.accuracy_score(
                predictions,
                y.iloc[test] if hasattr(y, "iloc") else y[test],
            )
        return accuracy / 5
    
class Landmark1NN():
    def __call__(self, X, y):
        import sklearn.neighbors

        if type(y) in ("binary", "multiclass"):
            kf = sklearn.model_selection.StratifiedKFold(n_splits=5)
        else:
            kf = sklearn.model_selection.KFold(n_splits=5)

        accuracy = 0.0
        for train, test in kf.split(X, y):
            kNN = sklearn.neighbors.KNeighborsClassifier(n_neighbors=1)
            if len(y.shape) == 1 or y.shape[1] == 1:
                kNN.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            else:
                kNN = OneVsRestClassifier(kNN)
                kNN.fit(
                    X.iloc[train] if hasattr(X, "iloc") else X[train],
                    y.iloc[train] if hasattr(y, "iloc") else y[train],
                )
            predictions = kNN.predict(
                X.iloc[test] if hasattr(X, "iloc") else X[test],
            )
            accuracy += sklearn.metrics.accuracy_score(
                predictions,
                y.iloc[test] if hasattr(y, "iloc") else y[test],
            )
        return accuracy / 5
    
class PCA():
    def __call__(self, X):
        import sklearn.decomposition

        pca = sklearn.decomposition.PCA(copy=True)
        rs = np.random.RandomState(42)
        indices = np.arange(X.shape[0])
        for i in range(10):
            try:
                rs.shuffle(indices)
                pca.fit(
                    X.iloc[indices] if hasattr(X, "iloc") else X[indices],
                )
                return pca
            except:
                pass
        self.logger.warning("Failed to compute a Principle Component Analysis")
        return None
    
class PCAFractionOfComponentsFor95PercentVariance():
    def __call__(self, X, PCA):
        pca_ = PCA
        if pca_ is None:
            return np.NaN
        sum_ = 0.0
        idx = 0
        while sum_ < 0.95 and idx < len(pca_.explained_variance_ratio_):
            sum_ += pca_.explained_variance_ratio_[idx]
            idx += 1
        return float(idx) / float(X.shape[1])
    
class PCAKurtosisFirstPC():
    def __call__(self, X, PCA):
        pca_ = PCA
        if pca_ is None:
            return np.NaN
        components = pca_.components_
        pca_.components_ = components[:1]
        transformed = pca_.transform(X)
        pca_.components_ = components

        kurtosis = scipy.stats.kurtosis(transformed)
        return kurtosis[0]
    
class PCASkewnessFirstPC():
    def __call__(self, X, PCA):
        pca_ = PCA
        if pca_ is None:
            return np.NaN
        components = pca_.components_
        pca_.components_ = components[:1]
        transformed = pca_.transform(X)
        pca_.components_ = components

        skewness = scipy.stats.skew(transformed)
        return skewness[0]