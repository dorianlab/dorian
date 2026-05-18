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
import { Plus, X } from "lucide-react";
import { useState } from "react";
import { Objective } from "@/types/session";
import Editor from "@monaco-editor/react";
import { ws } from "@/helpers/ws-events";

type OutputSpec = {
  name: string;
  type: IOType;
};
type IOType = "string" | "float";

const ALLOWED_LANGUAGES = ["python", "javascript"] as const;
const TYPE_OPTIONS: IOType[] = ["string", "float"];

function CustomEvaluationDialog({
  onAdd,
}: {
  onAdd: (objective: Objective) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [ioType, setIoType] = useState<IOType>("string");
  const [language, setLanguage] = useState<"python" | "javascript">("python");
  const [code, setCode] = useState("");
  const [outputs, setOutputs] = useState<OutputSpec[]>([]);

  const addOutput = () =>
    setOutputs((prev) => [...prev, { name: "", type: "string" }]);

  const updateOutput = <K extends keyof OutputSpec>(
    index: number,
    key: K,
    value: OutputSpec[K],
  ) =>
    setOutputs((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    );

  const removeOutput = (index: number) =>
    setOutputs((prev) => prev.filter((_, i) => i !== index));

  function handleAdd() {
    const trimmed = name.trim();
    const codeTrimmed = code.trim();
    if (!trimmed || !codeTrimmed) return;

    const uuid =
      crypto?.randomUUID?.() ??
      `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    // keep previous meta shape + NEW outputs
    const payload = {
      uuid,
      name: trimmed,
      meta: {
        ioType,
        language,
        code: codeTrimmed,
        outputs, // [{ name, type }]
      },
    } as unknown as Objective;

    onAdd(payload);

    ws.evaluationAdded({
      uuid,
      name: trimmed,
      ioType,
      language,
      code: codeTrimmed,
      outputs,
      ts: new Date().toISOString(),
    });

    // reset
    setName("");
    setIoType("string");
    setLanguage("python");
    setCode("");
    setOutputs([]);
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant='secondary' className='inline-flex items-center gap-2'>
          <Plus className='h-4 w-4' /> Custom Evaluation
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a custom evaluation procedure</DialogTitle>
          <DialogDescription>
            Give your evaluation procedure a short, clear name.
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
              height='220px'
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

          <div className='grid gap-2'>
            <Label>Outputs</Label>
            <div className='flex flex-col gap-2'>
              {outputs.map((row, i) => (
                <div
                  key={i}
                  className='grid grid-cols-1 md:grid-cols-12 gap-2 items-start'
                >
                  <Input
                    className='w-full md:col-span-6'
                    placeholder='Name'
                    value={row.name}
                    onChange={(e) => updateOutput(i, "name", e.target.value)}
                  />
                  <Select
                    value={row.type}
                    onValueChange={(v) => updateOutput(i, "type", v as IOType)}
                  >
                    <SelectTrigger className='w-full md:col-span-5'>
                      <SelectValue placeholder='Type' />
                    </SelectTrigger>
                    <SelectContent>
                      {TYPE_OPTIONS.map((opt) => (
                        <SelectItem key={opt} value={opt}>
                          {opt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>

                  <Button
                    type='button'
                    variant='ghost'
                    className='md:col-span-1 md:justify-self-end h-9 px-2'
                    onClick={() => removeOutput(i)}
                    aria-label='Remove output'
                    title='Remove'
                  >
                    <X className='h-4 w-4 text-red-500' />
                  </Button>
                </div>
              ))}
            </div>

            <Button type='button' variant='outline' onClick={addOutput}>
              <Plus className='h-4 w-4 mr-2' /> Add Output
            </Button>
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

export default CustomEvaluationDialog;
