package composition

type Task string

const (
  DataLoading Task = "data-loading"
  OneHotEncoding Task = "one-hot-encoding"
  MissingValueImputation Task = "missing-value-imputation"
  MinMaxScaling Task = "min-max-scaling"
  StandardScaling Task = "standard-scaling"
)
