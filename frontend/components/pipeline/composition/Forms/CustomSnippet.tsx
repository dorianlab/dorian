"use client";

import { useState } from "react";
import Editor from "@monaco-editor/react";
import { X } from "lucide-react";

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
import { cn } from "@/helpers/utils";
import { useTheme } from "next-themes";

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

export default function SnippetForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (data: {
    name: string;
    language: "python" | "javascript";
    code: string;
    inputs: InputSpec[];
    outputs: OutputSpec[];
  }) => void;
  onCancel: () => void;
}) {
  const { resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<"python" | "javascript">("python");
  const [code, setCode] = useState("");
  const [inputs, setInputs] = useState<InputSpec[]>([]);
  const [outputs, setOutputs] = useState<OutputSpec[]>([]);

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
  ) =>
    setInputs((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    );

  const updateOutput = <K extends keyof OutputSpec>(
    index: number,
    key: K,
    value: OutputSpec[K],
  ) =>
    setOutputs((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    );

  const removeInput = (index: number) =>
    setInputs((prev) => prev.filter((_, i) => i !== index));

  const removeOutput = (index: number) =>
    setOutputs((prev) => prev.filter((_, i) => i !== index));

  const monacoTheme = resolvedTheme === "dark" ? "vs-dark" : "vs";

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ name, language, code, inputs, outputs });
      }}
      className='flex flex-col gap-4'
    >
      <div className='flex w-auto  px-6 flex-col gap-5 pb-6 max-h-[450px] overflow-y-auto'>
        {/* Name */}
        <div className='flex flex-col gap-2.5'>
          <Label htmlFor='snippet-name'>Name</Label>
          <Input
            id='snippet-name'
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder='e.g. Compute Score'
          />
        </div>

        {/* Language */}
        <div className='flex flex-col gap-2.5'>
          <Label>Language</Label>
          <Select
            value={language}
            onValueChange={(v) => setLanguage(v as "python" | "javascript")}
          >
            <SelectTrigger>
              <SelectValue placeholder='Select language' />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value='python'>Python</SelectItem>
              <SelectItem value='javascript'>JavaScript</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {/* Code */}
        <div className='flex flex-col gap-2.5'>
          <Label>Code</Label>
          <div className='border rounded-md overflow-hidden'>
            <Editor
              height='220px'
              language={language}
              value={code}
              onChange={(val) => setCode(val || "")}
              key={monacoTheme}
              theme={monacoTheme}
              options={{
                fontSize: 14,
                minimap: { enabled: false },
                wordWrap: "on",
                automaticLayout: true,
              }}
            />
          </div>
        </div>

        {/* Inputs */}
        <div
          className={cn(
            "flex flex-col gap-2.5",
            inputs.length === 0 && "gap-1.5",
          )}
        >
          <Label>Inputs</Label>

          <div className='flex flex-col gap-2'>
            {inputs.map((row, i) => (
              <div
                key={i}
                className='grid grid-cols-1 md:grid-cols-12 gap-2 items-start'
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
            outputs.length === 0 && "gap-1.5",
          )}
        >
          <Label>Outputs</Label>

          <div className='flex flex-col gap-2'>
            {outputs.map((row, i) => (
              <div
                key={i}
                className='grid grid-cols-1 md:grid-cols-12 gap-2 items-start'
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

          <Button variant='outline' type='button' onClick={addOutput}>
            + Add Output
          </Button>
        </div>
      </div>

      <div className='flex px-6 py-4 border-t justify-end gap-2'>
        <Button variant='outline' type='button' onClick={onCancel}>
          Cancel
        </Button>
        <Button type='submit'>Save</Button>
      </div>
    </form>
  );
}
