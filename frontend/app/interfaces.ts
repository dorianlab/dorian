import { Elements } from 'react-flow-renderer';

interface Eval {
    name: string
}

interface Objective {
  id: string,
  name: string,
  codePen?: {
      language: string,
      code: string,
  }
  isCustom?: boolean
}

interface Task {
    name: string
}

interface Adapter {
  name: string
}

interface State {
  username: string,
  avatar: string,
  f: boolean,
  tasks: Task[],
  evals: Eval[],
  objectives: Objective[],
  adapters: Adapter[],
}

interface Profile {
  [key: string]: number;
}

interface Dataset {
  filename: string
  size: number
  hasLabels: boolean
  stage?: string
  progress?: number
  target?: string
  columns?: string[]
  profile?: Profile
}

interface Operator {
  name: string
  codePen?: {
      language: string,
      code: string,
  }
}

interface Query {
    name: string
}

interface Pipeline { 
    id: string,
    pipeline: Elements,
    pending?: boolean,
    performance?: number,
    done?: boolean
}

type Pipelines = Map<number, Pipeline[]>

export { State, Query, Eval, Objective, Task, Adapter, Dataset, Pipeline, type Pipelines, Operator }
