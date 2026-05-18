"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import TagsInput from "@/components/ui/tags-input";
import { X } from "lucide-react";
import { cn } from "@/helpers/utils";

const TASK_SUGGESTIONS = [
  "collect",
  "clean",
  "normalize",
  "validate",
  "transform",
  "feature engineering",
  "rank",
  "score",
  "train model",
  "evaluate",
  "aggregate",
  "join",
  "export",
  "log",
  "persist",
];

type IOType = "int" | "string" | "array" | "float" | "character" | "object";

type InputSpec = {
  name: string;
  type: IOType;
  defaultValue?: string;
};

type OutputSpec = {
  name: string;
  type: IOType;
};

const TYPE_OPTIONS: IOType[] = [
  "int",
  "string",
  "array",
  "float",
  "character",
  "object",
];

const LANG_OPTIONS = [
  "python",
  "javascript",
  "typescript",
  "go",
  "java",
] as const;

export default function OperatorForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (data: {
    name: string;
    language: string;
    inputs: InputSpec[];
    outputs: OutputSpec[];
    tasks: string[];
  }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [language, setLanguage] =
    useState<(typeof LANG_OPTIONS)[number]>("python");
  const [inputs, setInputs] = useState<InputSpec[]>([]);
  const [outputs, setOutputs] = useState<OutputSpec[]>([]);
  const [tasks, setTasks] = useState<string[]>([]);

  const addInput = () =>
    setInputs((prev) => [
      ...prev,
      { name: "", type: "string", defaultValue: "" },
    ]);

  const addOutput = () =>
    setOutputs((prev) => [...prev, { name: "", type: "string" }]);

  const updateInput = <K extends keyof InputSpec>(
    index: number,
    key: K,
    value: InputSpec[K],
  ) => {
    setInputs((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    );
  };

  const updateOutput = <K extends keyof OutputSpec>(
    index: number,
    key: K,
    value: OutputSpec[K],
  ) => {
    setOutputs((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    );
  };

  const removeInput = (index: number) =>
    setInputs((prev) => prev.filter((_, i) => i !== index));

  const removeOutput = (index: number) =>
    setOutputs((prev) => prev.filter((_, i) => i !== index));

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ name, language, inputs, outputs, tasks });
      }}
      className='flex flex-col gap-5 px-6'
    >
      <div className='flex flex-col gap-4 '>
        {/* Basic Info */}
        <div className='flex flex-col gap-2.5'>
          <Label htmlFor='op-name'>Name</Label>
          <Input
            id='op-name'
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder='e.g. Normalize Text'
          />
        </div>

        <div className='flex flex-col gap-2.5'>
          <Label>Language</Label>
          <Select value={language} onValueChange={(v) => setLanguage(v as any)}>
            <SelectTrigger>
              <SelectValue placeholder='Select language' />
            </SelectTrigger>
            <SelectContent>
              {LANG_OPTIONS.map((opt) => (
                <SelectItem key={opt} value={opt}>
                  {opt}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Inputs */}
        <div
          className={cn(
            "flex flex-col gap-2.5",
            inputs.length === 0 && "gap-1",
          )}
        >
          <Label>Inputs</Label>

          <div className='flex flex-col gap-2'>
            {inputs.map((row, i) => (
              <div
                key={i}
                className='grid grid-cols-1 gap-2 md:grid-cols-12 items-start'
              >
                <div className='md:col-span-4'>
                  <Input
                    placeholder='Name'
                    value={row.name}
                    onChange={(e) => updateInput(i, "name", e.target.value)}
                  />
                </div>

                <div className='md:col-span-3'>
                  <Select
                    value={row.type}
                    onValueChange={(v) => updateInput(i, "type", v as IOType)}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TYPE_OPTIONS.map((opt) => (
                        <SelectItem key={opt} value={opt}>
                          {opt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className='md:col-span-4'>
                  <Input
                    placeholder='Default value (optional)'
                    value={row.defaultValue ?? ""}
                    onChange={(e) =>
                      updateInput(i, "defaultValue", e.target.value)
                    }
                  />
                </div>

                <div className='md:col-span-1 md:justify-self-end'>
                  <Button
                    type='button'
                    variant='ghost'
                    size='icon'
                    onClick={() => removeInput(i)}
                    aria-label='Remove input'
                    title='Remove'
                  >
                    <X className='h-4 w-4' />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          <Button variant='outline' type='button' onClick={addInput}>
            + Add Input
          </Button>
        </div>

        {/* Outputs */}
        <div
          className={cn(
            "flex flex-col gap-2.5",
            outputs.length === 0 && "gap-1",
          )}
        >
          <Label>Outputs</Label>

          <div className='flex flex-col gap-2'>
            {outputs.map((row, i) => (
              <div
                key={i}
                className='grid grid-cols-1 gap-2 md:grid-cols-12 items-start'
              >
                <div className='md:col-span-6'>
                  <Input
                    placeholder='Name'
                    value={row.name}
                    onChange={(e) => updateOutput(i, "name", e.target.value)}
                  />
                </div>

                <div className='md:col-span-5'>
                  <Select
                    value={row.type}
                    onValueChange={(v) => updateOutput(i, "type", v as IOType)}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TYPE_OPTIONS.map((opt) => (
                        <SelectItem key={opt} value={opt}>
                          {opt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className='md:col-span-1 md:justify-self-end'>
                  <Button
                    type='button'
                    variant='ghost'
                    size='icon'
                    onClick={() => removeOutput(i)}
                    aria-label='Remove output'
                    title='Remove'
                  >
                    <X className='h-4 w-4' />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          <Button type='button' variant='outline' onClick={addOutput}>
            + Add Output
          </Button>
        </div>

        {/* Tasks */}
        <div className='flex flex-col gap-2.5'>
          <Label>Tasks</Label>
          <TagsInput
            value={tasks}
            onChange={setTasks}
            suggestions={TASK_SUGGESTIONS}
            placeholder='Type a task, press Enter (comma and paste supported)'
          />
        </div>
      </div>

      <div className='flex justify-end gap-2  py-4'>
        <Button variant='outline' type='button' onClick={onCancel}>
          Cancel
        </Button>
        <Button type='submit'>Save</Button>
      </div>
    </form>
  );
}
