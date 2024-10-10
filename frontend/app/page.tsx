"use client";

import React, { useState } from "react";
import Mock from "./components/mock";
import Sidebar from "./components/sidebar";
import getState from "./state";


export default function Home() {
  const ws = getState((state) => state.ws);
  const setName = getState((state) => state.init.setName);
  const setAvatar = getState((state) => state.init.setAvatar);
  const setDatasets = getState((state) => state.init.setDatasets);
  const setTasks = getState((state) => state.init.setTasks);
  const setAdapters = getState((state) => state.init.setAdapters);
  const setSelectedObjectives = getState((state) => state.current.setSelectedObjectives);
  const setObjectives = getState((state) => state.init.setObjectives);
  const setEvals = getState((state) => state.init.setEvals);
  const setQueries = getState((state) => state.init.setQueries);
  const setSelectedTask = getState((state) => state.current.setSelectedTask);
  const setSelectedEval = getState((state) => state.current.setSelectedEval);
  const setEnableDatasetUpload = getState((state) => state.current.setEnableDatasetUpload);
  const setEnableDatasetDelete = getState((state) => state.current.setEnableDatasetDelete);
  const setEnableTaskSelection = getState((state) => state.current.setEnableTaskSelection);
  const setEnableEvalSelection = getState((state) => state.current.setEnableEvalSelection);
  const setEnableObjectiveSelection = getState((state) => state.current.setEnableObjectiveSelection);
  const setEnableObjectiveDelete = getState((state) => state.current.setEnableObjectiveDelete);
  const setEnableObjectiveDragging = getState((state) => state.current.setEnableObjectiveDragging);

  
  ws.onopen = ev => {
    ws.send(JSON.stringify({
      command: 'init',
    }));
  };

  ws.onmessage = ev => {
    const resp = JSON.parse(ev.data);
    const {type, value} = resp;
    console.log(type, value);
    switch(type) {
      case "user/name": {
        setName(value);
        break;
      }
      case "user/avatar": {
        setAvatar(value);
        break;
      }
      case "state/tasks": {
        setTasks(value);
        break;
      }
      case "state/adapters": {
        setAdapters(value);
        break;
      }
      case "state/objectives": {
        setSelectedObjectives(value);
        break;
      }
      case "state/evals": {
        setEvals(value);
        break;
      }
      case "state/queries": {
        setQueries(value);
        break;
      }
      case "state/query": {
        setDatasets(value["datasets"]);
        setEnableDatasetUpload(value["toggles"]["dataset"]["add"]);
        setEnableDatasetDelete(value["toggles"]["dataset"]["delete"]);
        setEnableTaskSelection(value["toggles"]["task"]["select"]);
        setEnableEvalSelection(value["toggles"]["eval"]["select"]);
        setEnableObjectiveSelection(value["toggles"]["objectives"]["select"]);
        setEnableObjectiveDelete(value["toggles"]["objectives"]["select"]);
        setEnableObjectiveDragging(value["toggles"]["objectives"]["drag"]);
        setSelectedTask(value["task"]["name"]);
        setSelectedEval(value["eval"]["name"]);
        setObjectives(value["objectives"]);
        break;
      }
    }
  }

  return <>
    <main className="w-full pl-64 flex flex-col h-full">
      <Mock />
    </main>

    <aside className="fixed flex inset-y-0 w-64 h-full">
      <Sidebar />
    </aside>
  </>
}
