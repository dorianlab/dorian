import React, { useEffect, useState } from "react";
import { Dataset } from '../interfaces';
import { PaperClipIcon } from '@heroicons/react/20/solid';
import { Button } from "./button";
import getState from '../state';

import { Checkbox, CheckboxField, CheckboxGroup } from './checkbox'
import { Description, Fieldset, Field, Label, Legend } from './fieldset'
import { Select } from './select'
import { Text } from './text'

export default function DatasetUploader() {
    const datasets = getState((state) => state.datasets);
    const updateDataset = getState((state) => state.updateDataset);

    // function deleteDataset( did: string ) {
    //   const foundid = datasets.findIndex( (x: Dataset) => x.id === did);
    //   console.log(did, datasets, "deleting", foundid);
    //   (foundid >= 0) && setDatasets( (dd: Dataset[]) => {
    //     return (foundid >= 0) ? [
    //       ...dd.slice(0, foundid),
    //       ...dd.slice(foundid+1)
    //     ] : dd;
    //   } );
    // }

    // function humanFileSize(size: number) {
    //     var i = size == 0 ? 0 : Math.floor(Math.log(size) / Math.log(1024));
    //     return (size / Math.pow(1024, i)).toFixed(2) * 1 + ' ' + ['B', 'kB', 'MB', 'GB', 'TB'][i];
    // }

    return Array.isArray(datasets) && (datasets.length > 0) ? <>
        <ul role="list" className="divide-y divide-gray-100 rounded-md border border-gray-200">
            {
            datasets.map((dataset: Dataset) => (
                <li key={dataset.filename} className="items-center justify-between pt-1 text-sm leading-6">
                    <div className="flex flex-row items-center pb-1">
                        <PaperClipIcon className="h-5 w-5 flex-shrink-0 text-gray-400 mx-2" aria-hidden="true" />
                        <div className="flex flex-auto min-w-0 gap-2">
                            <span className="font-medium">{dataset.filename}</span>
                            {/* <span className="flex-shrink-0 text-gray-400">{humanFileSize(dataset.size)}</span> */}
                        </div>
                        <div className="flex gap-1 pr-1 transition ease-in-out duration-500 opacity-0 hover:opacity-100">
                            <Button outline>
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-6 h-6">
                                    <path strokeLinecap="round" strokeLinejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10" />
                                </svg>
                            </Button>
                            <Button outline>
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-6 h-6">
                                    <path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
                                </svg>
                            </Button>
                        </div>
                    </div>
                    <div className="flex flex-row">
                    <CheckboxGroup>
                        <CheckboxField>
                            <Checkbox name="hasLabels" onChange={(checked) => updateDataset(dataset.filename, "hasLabels", checked)} />
                            <Label>Has labels?</Label>
                        </CheckboxField>
                    </CheckboxGroup>
                    </div>
                    {(dataset.hasLabels && dataset.columns)
                    ? (
                        <div className="flex flex-row">
                            <Field>
                            <Label>Label Column</Label>
                            <Select name="label">
                                { dataset.columns!.map((column: string) => <option value={column}>column</option>) }
                            </Select>
                            </Field>
                        </div>
                    ) : (<></>)
                    }
                    {(dataset.progress && dataset.progress < 100)
                    ? (
                    <div className="w-full bg-gray-200 rounded-full h-1 dark:bg-gray-700">
                        <div className={`w-${dataset.progress}/12 bg-blue-600 h-1 rounded-full dark:bg-blue-500`}></div>
                    </div>
                    ) : (<></>)
                    }
                </li>
            ))
            }
        </ul>
    </> : <></>
}
