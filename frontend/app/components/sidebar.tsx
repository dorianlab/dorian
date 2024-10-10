import getState from '../state'
import { Button } from './button'
import { Field, Label, Fieldset, FieldGroup } from './fieldset'
import { Select } from './select'
import { upload } from '../../api/dataset'
import DatasetUploader from './dataset'
import { Dataset, Eval, Objective, Task } from '../interfaces'
import { useState } from 'react'
import { get } from 'http'


export default function Sidebar() {
    const url = getState((state) => state.backend);
    const tasks = getState((state) => state.known.tasks);
    const evals = getState((state) => state.known.evals);
    const objectives = getState((state) => state.known.objectives);
    const datasets = getState((state) => state.known.datasets);
    const addDatasets = getState((state) => state.known.addDatasets);
    const updateDataset = getState((state) => state.known.updateDataset);

    function classNames(...classes: string[]) {
        return classes.filter(Boolean).join(' ')
    }

    function setProgress( filename: string ) {
        return ( value: number ) => {
          updateDataset(filename, "progress", value)
        }
      }

    return <>
    <div className="flex grow flex-col gap-y-5 overflow-y-auto bg-white px-4 pb-2">
        <div className="flex h-16 shrink-0 items-center">
            <img
            className="h-10 w-auto"
            src={`${url}/favicon.ico`}
            alt="Dorian"
            />
            <h1 className="ml-1 text-2xl font-serif text-sky-900">Dorian</h1>
        </div>
        <nav className="flex flex-1 flex-col">
            <Fieldset>
            <FieldGroup>
            <Field>
                <Label>Datasets</Label>
                <br/>
                <Button outline className="w-full mt-3" onClick={() => document.getElementById('file-upload')?.click()}>
                    Upload
                    <input
                    id="file-upload"
                    type="file"
                    onChange={async (e) => {
                        e.preventDefault();
                        console.log(e.target.files);
                        if (e.target.files?.length) {
                            for (let i = 0; i < e.target.files.length; i++) {
                            let file = e.target.files?.item(i)!;
                            addDatasets([{
                                    filename: file.name,
                                    size: file.size,
                                    hasLabels: false,
                                    progress: 0
                                }
                            ]);
                            await upload(file, setProgress(file.name));
                            }
                        }
                    }}
                    multiple
                    hidden
                />
                </Button>
            </Field>
            <DatasetUploader values={datasets}></DatasetUploader>
            <Field>
                <Label>Data Science Task</Label>
                <Select name="task">
                {tasks ?? tasks.map((task: Task) => (
                    <option value={task.name}>{task.name}</option>
                ))
                }
                </Select>
            </Field>
            <Field>
                <Label>Pipeline</Label>
                <Button outline className="w-full mt-3" disabled>Import</Button>
                <Button outline className="w-full mt-3" disabled>Compose</Button>
            </Field>
            <Field>
                <Label>Evaluation Process</Label>
                <Select name="eval">
                {evals ?? evals.map((procedure: Eval) => (
                    <option value={procedure.name}>{procedure.name}</option>
                ))
                }
                </Select>
            </Field>
            <Field>
                <Label>Ranking Objectives</Label>
                <Select name="objectives">
                {objectives ?? objectives.map((objective: Objective) => (
                    <option value={objective.name}>{objective.name}</option>
                ))
                }
                </Select>
            </Field>
            </FieldGroup>
            </Fieldset>
        </nav>
    </div>   
    </>
}