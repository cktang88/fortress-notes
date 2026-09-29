import { useState } from "react";

interface Props {
  blockId: string;
  values: Record<string, unknown>;
  saving: boolean;
  onSave: (values: Record<string, unknown>) => Promise<void>;
}

export function BlockPropertiesEditor({ blockId, values, saving, onSave }: Props) {
  const tags = stringList(values.tags);
  const attributes = Object.entries(values).filter(([name]) => name !== "tags");
  const [newTag, setNewTag] = useState("");
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");

  const saveTag = (tag: string, action: "add" | "remove") => {
    const value = tag.trim();
    if (!value) return;
    const next = action === "add"
      ? (tags.includes(value) ? tags : [...tags, value])
      : tags.filter((existing) => existing !== value);
    void onSave({ ...values, tags: next });
    setNewTag("");
  };

  const saveAttribute = (name: string, value: unknown) => {
    const key = name.trim();
    if (!key || key === "tags") return;
    void onSave({ ...values, [key]: value });
  };

  const removeAttribute = (name: string) => {
    const next = { ...values };
    delete next[name];
    void onSave(next);
  };

  return (
    <details className="mx-8 mt-3 rounded border border-zinc-200 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium text-zinc-600">
        Block tags and attributes
      </summary>
      <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="Block tags">
        {tags.map((tag) => (
          <span key={tag} className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2 py-1">
            {tag}
            <button
              type="button"
              aria-label={`Remove block tag ${tag}`}
              disabled={saving}
              onClick={() => saveTag(tag, "remove")}
              className="text-zinc-400 hover:text-zinc-700"
            >
              ×
            </button>
          </span>
        ))}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            saveTag(newTag, "add");
          }}
          className="flex items-center gap-1"
        >
          <label className="sr-only" htmlFor={`block-tag-${blockId}`}>Add block tag</label>
          <input
            id={`block-tag-${blockId}`}
            value={newTag}
            onChange={(event) => setNewTag(event.target.value)}
            placeholder="Add tag"
            className="w-24 rounded border border-zinc-200 px-2 py-1"
          />
          <button type="submit" disabled={saving} className="rounded border px-2 py-1">
            Add tag
          </button>
        </form>
      </div>

      {attributes.length > 0 && (
        <ul className="mt-3 space-y-2" aria-label="Block attributes">
          {attributes.map(([name, value]) => (
            <AttributeRow
              key={name}
              name={name}
              value={displayValue(value)}
              disabled={saving}
              onSave={(next) => saveAttribute(name, next)}
              onRemove={() => removeAttribute(name)}
            />
          ))}
        </ul>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          saveAttribute(newName, newValue);
          setNewName("");
          setNewValue("");
        }}
        className="mt-3 flex flex-wrap items-center gap-2"
      >
        <label className="sr-only" htmlFor={`block-attribute-name-${blockId}`}>Attribute name</label>
        <input
          id={`block-attribute-name-${blockId}`}
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
          placeholder="Attribute"
          className="w-32 rounded border border-zinc-200 px-2 py-1"
        />
        <label className="sr-only" htmlFor={`block-attribute-value-${blockId}`}>Attribute value</label>
        <input
          id={`block-attribute-value-${blockId}`}
          value={newValue}
          onChange={(event) => setNewValue(event.target.value)}
          placeholder="Value"
          className="w-40 rounded border border-zinc-200 px-2 py-1"
        />
        <button type="submit" disabled={saving} className="rounded border px-2 py-1">
          Add attribute
        </button>
      </form>
    </details>
  );
}

function AttributeRow({
  name,
  value,
  disabled,
  onSave,
  onRemove,
}: {
  name: string;
  value: string;
  disabled: boolean;
  onSave: (value: string) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = useState(value);

  return (
    <li className="flex flex-wrap items-center gap-2">
      <span className="min-w-24 font-medium text-zinc-600">{name}</span>
      <input
        aria-label={`Value for ${name}`}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        className="w-40 rounded border border-zinc-200 px-2 py-1"
      />
      <button type="button" disabled={disabled || draft === value} onClick={() => onSave(draft)} className="rounded border px-2 py-1">
        Save
      </button>
      <button type="button" disabled={disabled} onClick={onRemove} className="rounded border px-2 py-1">
        Remove
      </button>
    </li>
  );
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function displayValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}
