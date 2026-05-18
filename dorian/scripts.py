scripts = [
    """
from matplotlib import pyplot as plt

plt.show(X)
    """,
    """
import pandas as pd

# Load the COMPAS dataset
# Note: Adjust the path to the location of your COMPAS dataset
data = pd.read_csv('compas-scores-two-years.csv')
    """,
    """
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
import pandas as pd

data = pd.read_csv('compas-scores-two-years.csv')

# Selecting features and target variable
# Features to be used for classification (adjust as necessary)
features = [
    'age', 'sex', 'race', 'juv_fel_count', 'juv_misd_count', 'juv_other_count',
    'priors_count', 'days_b_screening_arrest', 'c_charge_degree'
]

# Target variable (binary classification: recidivism within two years)
target = 'two_year_recid'

# Preprocessing
# Convert categorical features to numeric using one-hot encoding
df = pd.get_dummies(data, columns=['sex', 'race', 'c_charge_degree'], drop_first=True)

# Extract features and target from the data
X = df[features]
y = df[target]

# Split the data into training and test sets
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

# Standardize the features (mean=0, variance=1)
scaler = StandardScaler()
X_train = scaler.fit_transform(X_train)
X_test = scaler.transform(X_test)

# Initialize and train the classifier
classifier = LogisticRegression(random_state=42)
classifier.fit(X_train, y_train)

# Predict on the test set
y_pred = classifier.predict(X_test)

# Evaluate the classifier
accuracy = accuracy_score(y_test, y_pred)
conf_matrix = confusion_matrix(y_test, y_pred)
class_report = classification_report(y_test, y_pred)

# Print evaluation results
print(f'Accuracy: {accuracy:.2f}')
print('Confusion Matrix:')
print(conf_matrix)
print('Classification Report:')
print(class_report)
    """,
    """
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report


# Load the COMPAS dataset
# Note: Adjust the path to the location of your COMPAS dataset
data = pd.read_csv('compas-scores-two-years.csv')

# Selecting features and target variable
# Features to be used for classification (adjust as necessary)
features = [
    'age', 'sex', 'race', 'juv_fel_count', 'juv_misd_count', 'juv_other_count',
    'priors_count', 'days_b_screening_arrest', 'c_charge_degree'
]

# Target variable (binary classification: recidivism within two years)
target = 'two_year_recid'

# Splitting the data into features and target
X = data[features]
y = data[target]

# Preprocessing
# Define the categorical and numerical columns
categorical_features = ['sex', 'race', 'c_charge_degree']
numerical_features = [
    'age', 'juv_fel_count', 'juv_misd_count', 'juv_other_count', 'priors_count', 'days_b_screening_arrest'
]

# DEBUG: REMOVE LATER
# my_list = [1,2, [3,4], [[5]], [[[6], [7]]]]

# Preprocessing for numerical data
numerical_transformer = StandardScaler()

# Preprocessing for categorical data
categorical_transformer = OneHotEncoder(drop='first')

# Combine preprocessing steps
preprocessor = ColumnTransformer(
    transformers=[
        ('num', numerical_transformer, numerical_features),
        ('cat', categorical_transformer, categorical_features)
    ])

# Create a pipeline that combines preprocessing with the classifier
pipeline = Pipeline(steps=[
    ('preprocessor', preprocessor),
    ('classifier', LogisticRegression(random_state=42))
])

# Split the data into training and test sets
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

# Train the model
pipeline.fit(X_train, y_train)

# Predict on the test set
y_pred = pipeline.predict(X_test)

# Evaluate the classifier
accuracy = accuracy_score(y_test, y_pred)
conf_matrix = confusion_matrix(y_test, y_pred)
class_report = classification_report(y_test, y_pred)

# Print evaluation results
print(f'Accuracy: {accuracy:.2f}')
print('Confusion Matrix:')
print(conf_matrix)
print('Classification Report:')
print(class_report)
    """,
]

debug_scripts = [
    """
    categorical_features = ['sex', 'race', 'c_charge_degree']
    numerical_features = [
        'age', 'juv_fel_count', 'juv_misd_count', 'juv_other_count', 'priors_count', 'days_b_screening_arrest'
    ]
    """
]
