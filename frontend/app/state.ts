import { create } from 'zustand'
import { Dataset } from './interfaces'
import config from '../env.config'

interface Toggles {
    DatasetUpload: boolean
    DatasetDelete: boolean
    TaskSelection: boolean
    EvalSelection: boolean
    ObjectiveSelection: boolean
    ObjectiveDelete : boolean
    ObjectiveDragging: boolean
    // : Boolean
}

interface Current {
    username?: string
    task?: string
    eval?: string
    avatar?: string
    queries: string[]
    setSelectedObjectives: (objectives: string[]) => void
    setSelectedTask: (task: string) => void
    setSelectedEval: (evaluation: string) => void
    toggles: Toggles
    setEnableDatasetUpload: (toggle: boolean) => void
    setEnableDatasetDelete: (toggle: boolean) => void
    setEnableTaskSelection: (toggle: boolean) => void
    setEnableEvalSelection: (toggle: boolean) => void
    setEnableObjectiveSelection: (toggle: boolean) => void
    setEnableObjectiveDelete: (toggle: boolean) => void
    setEnableObjectiveDragging: (toggle: boolean) => void
}

interface Known {
    tasks: string[]
    datasets: Dataset[]
    evals: string[]
    objectives: string[]
    adapters: string[]
    addDatasets: (datasets: Dataset[]) => void
    // updateDataset: (dataset: Dataset) => void
    updateDataset: <K extends keyof Dataset>(filename: string, key: K, value: Dataset[K]) => void
    addTasks: (tasks: string[]) => void
    addEvals: (evals: string[]) => void
    addObjectives: (objectives: string[]) => void
}

interface Init {
    setName: (name: string) => void
    setAvatar: (url: string) => void
    setDatasets: (datasets: Dataset[]) => void
    setTasks: (tasks: string[]) => void
    setEvals: (evals: string[]) => void
    setAdapters: (adapters: string[]) => void
    setQueries: (queries: string[]) => void
    setObjectives: (objectives: string[]) => void
    setDatasetUpload: (toggle: boolean) => void
    setDatasetDelete: (toggle: boolean) => void
    setTaskSelection: (toggle: boolean) => void
    setEvalSelection: (toggle: boolean) => void
    setObjectiveSelection: (toggle: boolean) => void
    setObjectiveDelete: (toggle: boolean) => void
    setObjectiveDragging: (toggle: boolean) => void
}

interface State {
    session?: string
    backend: string
    ws: WebSocket
    reconnect: () => void
    known: Known
    current: Current
    init: Init
}

const getState = create<State>()((set) => ({
    session: undefined,
    backend: config.backend,
    ws: new WebSocket(config.ws),
    reconnect: () => set((state) => ({ ws: new WebSocket(config.ws) })),
    known: {
        datasets: [],
        tasks: [],
        evals: [],
        objectives: [],
        adapters: [],
        addDatasets: (datasets: Dataset[]) => set((state) => ({ known: { ...state.known, datasets: [...state.known.datasets, ...datasets]}})),
        updateDataset: (filename, key, value) => set((state) => ({ known: { ...state.known, datasets: state.known.datasets.map(d => {
            if (d.filename === filename) d[key] = value
            return d
        })}})),
        addTasks: (tasks: string[]) => set((state) => ({ known: { ...state.known, tasks: [...state.known.tasks, ...tasks] }})),
        addEvals: (evals) => set((state) => ({ known: { ...state.known, evals: [...state.known.evals, ...evals] }})),
        addObjectives: (objectives) => set((state) => ({ known: { ...state.known, objectives: [...state.known.objectives, ...objectives] }})),
    },
    current: {
        username: undefined,
        task: undefined,
        avatar: undefined,
        eval: undefined,
        objectives: [],
        queries: [],
        setSelectedObjectives: (objectives: string[]) => set((state) => ({ current: { ...state.current, objectives: objectives }})),
        setSelectedTask: (task: string) => set((state) => ({ current: { ...state.current, task: task }})),
        setSelectedEval: (evaluation: string) => set((state) => ({ current: { ...state.current, eval: evaluation }})),
        toggles: {
            DatasetUpload: false,
            DatasetDelete: false,
            TaskSelection: false,
            EvalSelection: false,
            ObjectiveSelection: false,
            ObjectiveDelete : false,
            ObjectiveDragging: false,
        },
        setEnableDatasetUpload: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, DatasetUpload: toggle} }})),
        setEnableDatasetDelete: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, DatasetDelete: toggle} }})),
        setEnableTaskSelection: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, TaskSelection: toggle} }})),
        setEnableEvalSelection: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, EvalSelection: toggle} }})),
        setEnableObjectiveSelection: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, ObjectiveSelection: toggle} }})),
        setEnableObjectiveDelete: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, ObjectiveDelete: toggle} }})),
        setEnableObjectiveDragging: (toggle: boolean) => set((state) => ({ current: { ...state.current, toggles: {...state.current.toggles, ObjectiveDragging: toggle} }})),
    },
    init: {
        setName: (name: string) => set((state) => ({current: {...state.current, username: name}})),
        setAvatar: (url: string) => set((state) => ({current: {...state.current, avatar: url}})),
        setDatasets: (datasets: Dataset[]) => set((state) => ({ known: { ...state.known, datasets: datasets}})),
        setTasks: (tasks: string[]) => set((state) => ({ known: { ...state.known, tasks: tasks}})),      
        setAdapters: (adapters: string[]) => set((state) => ({ known: { ...state.known, adapters: adapters}})),
        setEvals: (evals: string[]) => set((state) => ({current: {...state.current, evals: evals}})),
        setQueries: (queries: string[]) => set((state) => ({current: {...state.current, queries: queries}})),
        setObjectives: (objectives: string[]) => set((state) => ({ known: { ...state.known, objectives: objectives}})),      
        setDatasetUpload: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, DatasetUpload: toggle}}})),
        setDatasetDelete: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, DatasetDelete: toggle}}})),
        setTaskSelection: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, TaskSelection: toggle}}})),
        setEvalSelection: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, EvalSelection: toggle}}})),
        setObjectiveSelection: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, ObjectiveSelection: toggle}}})),
        setObjectiveDelete: (toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, ObjectiveDelete: toggle}}})),
        setObjectiveDragging:(toggle: boolean) => set((state) => ({current: {...state.current, toggles: {...state.current.toggles, ObjectiveDragging: toggle}}})),
    }
}))

export default getState;