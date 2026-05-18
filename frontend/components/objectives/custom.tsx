import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Plus } from "lucide-react";
import React, { useState } from "react";
import { Objective } from "@/types/session";
import Editor from "@monaco-editor/react";
import { randomUUID } from "@/helpers/uuid";

const ALLOWED_LANGUAGES = ["python", "javascript"] as const;

const CODE_TEMPLATE = `def score(candidate, ctx):
    # candidate["evaluations"] — list of {score: float}
    # candidate["operators"]   — list of {name: str}
    # ctx.dataset_profile      — dict or None
    # ctx.current_pipeline     — dict or None
    # ctx.task                 — str or None
    # Return a float (higher = better)
    return 0.0
`;

function CustomObjectiveDialog({
  onAdd,
}: {
  onAdd: (objective: Objective) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<"python" | "javascript">("python");
  const [code, setCode] = useState(CODE_TEMPLATE);

  function handleAdd() {
    const trimmed = name.trim();
    const codeTrimmed = code.trim();
    if (!trimmed || !codeTrimmed) return;
    const uuid = randomUUID();
    onAdd({
      uuid,
      name: trimmed,
      language,
      code: codeTrimmed,
      type: "snippet",
    } as Objective);
    setName("");
    setCode(CODE_TEMPLATE);
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant='secondary' className='inline-flex items-center gap-2'>
          <Plus className='h-4 w-4' /> Custom Objective
        </Button>
      </DialogTrigger>
      <DialogContent className='max-w-3xl'>
        <DialogHeader>
          <DialogTitle>Add a custom objective</DialogTitle>
          <DialogDescription>
            Define a scoring function that ranks pipeline candidates.
            Your code must define <code>score(candidate, ctx) → float</code>.
          </DialogDescription>
        </DialogHeader>
        <div className='grid gap-2 py-2'>
          <Label htmlFor='objective-name'>Name</Label>
          <Input
            id='objective-name'
            placeholder='e.g. Improve onboarding flow'
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleAdd();
            }}
          />
        </div>
        <div className='grid gap-2'>
          <Label>Language</Label>
          <Select
            value={language}
            onValueChange={(v) => setLanguage(v as typeof language)}
          >
            <SelectTrigger>
              <SelectValue placeholder='Select language' />
            </SelectTrigger>
            <SelectContent>
              {ALLOWED_LANGUAGES.map((lang) => (
                <SelectItem className='capitalize' key={lang} value={lang}>
                  {lang}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className='grid gap-2'>
          <Label>Code </Label>
          <div className='border rounded overflow-hidden'>
            <Editor
              height='400px'
              language={language}
              value={code}
              onChange={(val) => setCode(val || "")}
              theme='light'
              options={{
                fontSize: 14,
                minimap: { enabled: false },
                wordWrap: "on",
                automaticLayout: true,
              }}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant='outline' onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button disabled={!name.trim() || !code.trim()} onClick={handleAdd}>
            Add
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default CustomObjectiveDialog;
