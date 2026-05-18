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

export default function ParameterForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (data: { name: string; type: string; value: string }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState("int");
  const [value, setValue] = useState("");

  const isEnv = type === "env";

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ name, type, value: isEnv ? "" : value });
      }}
      className='flex flex-col gap-5 px-6'
    >
      <div className='flex flex-col gap-4 '>
        <div className='flex flex-col gap-2.5'>
          <Label htmlFor='param-name'>Name</Label>
          <Input
            id='param-name'
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder={isEnv ? "e.g. OPENROUTER_API_KEY" : "e.g. bootstrap"}
          />
        </div>

        <div className='flex flex-col gap-2.5'>
          <Label>Type</Label>
          <Select value={type} onValueChange={(v) => { setType(v); if (v === "env") setValue(""); }}>
            <SelectTrigger>
              <SelectValue placeholder='Select type' />
            </SelectTrigger>
            <SelectContent className='z-[60]'>
              <SelectItem value='int'>int</SelectItem>
              <SelectItem value='float'>float</SelectItem>
              <SelectItem value='str'>str</SelectItem>
              <SelectItem value='env'>env (vault secret)</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {isEnv ? (
          <p className='text-xs text-muted-foreground'>
            The env variable will be selected from your vault after dropping the node onto the canvas.
          </p>
        ) : (
          <div className='flex flex-col gap-2.5'>
            <Label htmlFor='param-default'>Default Value</Label>
            <Input
              id='param-default'
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder='Optional'
            />
          </div>
        )}
      </div>

      <div className='flex justify-end gap-2 py-4'>
        <Button variant='outline' type='button' onClick={onCancel}>
          Cancel
        </Button>
        <Button type='submit'>Save</Button>
      </div>
    </form>
  );
}
