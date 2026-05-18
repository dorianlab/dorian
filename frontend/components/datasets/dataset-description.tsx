"use client";

/**
 * DatasetDescription
 * ------------------
 * Renders the human-authored ``description`` field from a dataset document.
 *
 * OpenML ships descriptions as pseudo-markdown with a short key/value header
 * (``**Author**: ...  \n**Source**: [link](url)``) followed by prose, lists,
 * and ``### Headings``.  Rendering the raw string as a single blob of text
 * (as the old detail view did) produces the wall-of-markdown look from the
 * original screenshot.
 *
 * This component parses the OpenML header into structured chips and renders
 * the body with minimal markdown support — paragraphs, headings, bullets,
 * bold, and simple links — without pulling in ``react-markdown``.  It also
 * supports plain-text descriptions entered by end users via the upload
 * dialog: for those we just render paragraphs.
 */

import { useState, useMemo } from "react";
import { ChevronDown, ChevronRight, Pencil, Check, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  text: string;
  /** Initial number of body paragraphs shown before "Show more". */
  collapsedParagraphs?: number;
  /** If set, the panel shows an edit affordance and calls this on save. */
  onSave?: (next: string) => Promise<void> | void;
  /** Whether the current viewer owns the dataset (required for editing). */
  editable?: boolean;
}

interface HeaderField {
  key: string;
  value: string;
  href?: string;
}

interface ParsedDescription {
  header: HeaderField[];
  body: string;
}

/** Known header keys (lower-cased) that deserve chip rendering. */
const HEADER_KEYS = new Set([
  "author",
  "authors",
  "source",
  "sources",
  "please cite",
  "citation",
  "date",
  "upload date",
  "donor",
  "contributor",
  "contributors",
  "creator",
  "creators",
  "collection date",
  "version",
  "license",
  "url",
  "reference",
]);

const HEADER_LINE_RE = /^\*\*([^*]+?)\*\*:\s*(.*?)\s*$/;
const MD_LINK_RE = /\[([^\]]+)\]\(([^)]+)\)/;

/** Extract structured `**Key**: value` lines from the top of a description. */
function parseOpenMLDescription(text: string): ParsedDescription {
  const lines = text.split(/\r?\n/);
  const header: HeaderField[] = [];
  let i = 0;
  // Header fields are contiguous at the top, separated by blank lines only.
  while (i < lines.length) {
    const raw = lines[i].trim();
    if (raw === "") {
      i += 1;
      continue;
    }
    const m = raw.match(HEADER_LINE_RE);
    if (!m) break;
    const key = m[1].trim();
    if (!HEADER_KEYS.has(key.toLowerCase())) break;

    let value = m[2];
    // "(none)" / "None" → drop the field; no signal.
    const stripped = value.replace(/[\s.]+$/, "").toLowerCase();
    if (
      stripped === "none" ||
      stripped === "(none)" ||
      stripped === "n/a" ||
      stripped === ""
    ) {
      i += 1;
      continue;
    }

    // Extract a single markdown link if present → use text + href.
    const linkMatch = value.match(MD_LINK_RE);
    let href: string | undefined;
    if (linkMatch) {
      href = linkMatch[2];
      value = value.replace(MD_LINK_RE, linkMatch[1]);
    }
    header.push({ key, value: value.trim(), href });
    i += 1;
  }

  // Remaining lines form the body.  Collapse runs of blank lines.
  const bodyLines = lines.slice(i);
  const body = bodyLines
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return { header, body };
}

/** Split body into blocks (paragraphs, headings, bullet lists). */
type Block =
  | { type: "p"; text: string }
  | { type: "h"; level: 2 | 3 | 4; text: string }
  | { type: "ul"; items: string[] };

function parseBody(body: string): Block[] {
  if (!body) return [];
  const blocks: Block[] = [];
  // Split on blank lines → candidate blocks.
  const chunks = body.split(/\n{2,}/);
  for (const chunk of chunks) {
    const trimmed = chunk.trim();
    if (!trimmed) continue;

    // Heading (###, ####, or ## at start of chunk).
    const headingMatch = trimmed.match(/^(#{2,4})\s+(.*)$/);
    if (headingMatch) {
      const level = Math.min(4, Math.max(2, headingMatch[1].length)) as 2 | 3 | 4;
      blocks.push({ type: "h", level, text: headingMatch[2].trim() });
      continue;
    }

    // Bullet list: every non-empty line starts with "* " or "- ".
    const lines = trimmed.split(/\n/).map((l) => l.trim()).filter(Boolean);
    if (lines.length > 0 && lines.every((l) => /^[*-]\s+/.test(l))) {
      blocks.push({
        type: "ul",
        items: lines.map((l) => l.replace(/^[*-]\s+/, "")),
      });
      continue;
    }

    // Paragraph: flatten soft line-breaks inside the chunk to a single space.
    blocks.push({ type: "p", text: trimmed.replace(/\s*\n\s*/g, " ") });
  }
  return blocks;
}

/** Render a string with inline **bold** and [link](url) markdown. */
function renderInline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let i = 0;
  const re = /\*\*([^*]+?)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let match: RegExpExecArray | null;
  let lastIndex = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      out.push(text.slice(lastIndex, match.index));
    }
    if (match[1] !== undefined) {
      out.push(
        <strong key={i++} className="font-semibold">
          {match[1]}
        </strong>,
      );
    } else if (match[2] !== undefined && match[3] !== undefined) {
      out.push(
        <a
          key={i++}
          href={match[3]}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline underline-offset-2 hover:text-primary/80"
        >
          {match[2]}
        </a>,
      );
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) out.push(text.slice(lastIndex));
  return out;
}

export function DatasetDescription({
  text,
  collapsedParagraphs = 2,
  onSave,
  editable = false,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const [saving, setSaving] = useState(false);

  const { header, body } = useMemo(() => parseOpenMLDescription(text), [text]);
  const blocks = useMemo(() => parseBody(body), [body]);

  const hasContent = header.length > 0 || blocks.length > 0;

  // Empty + editable: show an "Add description" placeholder button.
  if (!hasContent && !editing) {
    if (!editable || !onSave) return null;
    return (
      <Button
        variant="outline"
        size="sm"
        className="h-8 gap-1.5 text-xs text-muted-foreground"
        onClick={() => {
          setDraft("");
          setEditing(true);
        }}
      >
        <Pencil className="h-3 w-3" />
        Add description
      </Button>
    );
  }

  if (editing && onSave) {
    const commit = async () => {
      setSaving(true);
      try {
        await onSave(draft);
        setEditing(false);
      } finally {
        setSaving(false);
      }
    };
    return (
      <div className="space-y-2 max-w-3xl">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Describe this dataset — source, columns, provenance, caveats…"
          className="min-h-[140px] text-sm"
          disabled={saving}
          autoFocus
        />
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={commit} disabled={saving} className="h-7 gap-1.5 text-xs">
            <Check className="h-3 w-3" />
            Save
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setDraft(text);
              setEditing(false);
            }}
            disabled={saving}
            className="h-7 gap-1.5 text-xs"
          >
            <X className="h-3 w-3" />
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  if (!hasContent) return null;

  // Decide how much of the body to show when collapsed.  Count paragraphs
  // only — headings and bullets ride along with the paragraph they follow.
  const paragraphCount = blocks.filter((b) => b.type === "p").length;
  const shouldCollapse = paragraphCount > collapsedParagraphs;

  let visibleBlocks = blocks;
  if (shouldCollapse && !expanded) {
    const limit = collapsedParagraphs;
    let seen = 0;
    const cut: Block[] = [];
    for (const b of blocks) {
      if (b.type === "p") {
        if (seen >= limit) break;
        seen += 1;
      }
      cut.push(b);
    }
    visibleBlocks = cut;
  }

  return (
    <div className="space-y-4">
      {header.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {header.map((f) => {
            const chip = (
              <Badge
                variant="secondary"
                className="gap-1.5 font-normal text-[11px] py-0.5 px-2"
              >
                <span className="text-muted-foreground">{f.key}:</span>
                <span className="text-foreground">{f.value}</span>
              </Badge>
            );
            if (f.href) {
              return (
                <a
                  key={f.key}
                  href={f.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="no-underline hover:opacity-80"
                >
                  {chip}
                </a>
              );
            }
            return <span key={f.key}>{chip}</span>;
          })}
        </div>
      )}

      {visibleBlocks.length > 0 && (
        <div className="space-y-3 text-sm text-muted-foreground leading-relaxed max-w-3xl">
          {visibleBlocks.map((b, idx) => {
            if (b.type === "h") {
              const sizeCls =
                b.level === 2
                  ? "text-base"
                  : b.level === 3
                    ? "text-sm"
                    : "text-xs";
              return (
                <h3
                  key={idx}
                  className={`${sizeCls} font-semibold text-foreground pt-1`}
                >
                  {renderInline(b.text)}
                </h3>
              );
            }
            if (b.type === "ul") {
              return (
                <ul
                  key={idx}
                  className="list-disc list-outside pl-5 space-y-1 marker:text-muted-foreground/60"
                >
                  {b.items.map((it, j) => (
                    <li key={j}>{renderInline(it)}</li>
                  ))}
                </ul>
              );
            }
            return <p key={idx}>{renderInline(b.text)}</p>;
          })}
        </div>
      )}

      <div className="flex items-center gap-1">
        {shouldCollapse && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 -ml-2 gap-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? (
              <>
                <ChevronDown className="h-3.5 w-3.5" />
                Show less
              </>
            ) : (
              <>
                <ChevronRight className="h-3.5 w-3.5" />
                Show more
              </>
            )}
          </Button>
        )}
        {editable && onSave && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => {
              setDraft(text);
              setEditing(true);
            }}
          >
            <Pencil className="h-3 w-3" />
            Edit
          </Button>
        )}
      </div>

    </div>
  );
}
