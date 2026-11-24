// Agent avatar editor — parity port of the original dashboard's avatarEditor /
// openCharacterGenerate chain. Three ways to set the look:
//   • Upload photo  → POST   /api/agents/{name}/avatar         (multipart)
//   • Use emoji     → DELETE /api/agents/{name}/avatar
//   • Generate cartoon → POST /api/agents/{name}/character/generate (multipart)
//        portrait + two 4×4 sprite pages. Runs on the Olano image service
//        (credits, no key) by default; the Image engine select below lets the
//        user send it to their own OpenAI account instead. Backends come from
//        GET /api/avatar/models, which marks exactly one as `default`.
//
// IMPORTANT: the actual image prompts live server-side in
// olano/sprites/prompts.py and are applied automatically by the
// /character/generate endpoint — this UI only *mirrors* them read-only (the
// CHARGEN_* constants below are copied verbatim from olano/sprites/prompts.py,
// PAGE1_PROMPT / PAGE2_PROMPT) and forwards the user's description / style /
// emoji / reference exactly as the original dashboard did. Do not change the
// request shape; keep the mirrored text in sync with prompts.py.
//
// Do NOT add a cache-buster to the avatar or sprite <img> srcs. This file used
// to append `?t=${Date.now()}` so the preview refreshed after a regenerate; the
// API now stamps every media URL with `?v=<content>`, so a new portrait already
// arrives as a different URL. A timestamp on top re-downloads the image on
// every mount and malforms the query — it defeats exactly the caching the stamp
// exists to enable.

import { useEffect, useRef, useState } from "react";
import { Upload, Sparkles, X } from "lucide-react";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { ApiError, api } from "../../core/api";
import { Modal, Field, ConfirmModal } from "./Modal";
import { STR } from "./strings";

// ── Locked technical prompts — mirrored verbatim from olano/sprites/prompts.py
//    (PAGE1_PROMPT / PAGE2_PROMPT). Shown read-only so the user sees exactly
//    what's baked in; the server applies the real prompts on generate. ────────
const CHARGEN_PAGE1_PROMPT = `Use the uploaded reference image as the identity reference for the SAME character across the entire sprite sheet.

STYLE TARGET:
Match the cute, simple, less-detailed top-down RPG pixel sprite style of the previous Page 2 v2 result:
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly

IMPORTANT:
Preserve the recognizable identity and species traits from the reference image while transforming the character into a humanoid office worker suitable for a top-down RPG game.

The character should:
- stand upright on two legs
- have cute chibi human-like body proportions
- wear the SAME casual office-worker clothes in every frame
- maintain the recognizable identity and silhouette from the reference
- keep the same head size, same face, same beard/markings/features, same clothing, same body scale

4-FRAME-WALK-RULES
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

CLOTHING LOCK:
Use the exact same outfit in every frame:
Do not change to shirt, pants, dress or shoes.

SHEET FORMAT:
Exactly 4 rows and 4 columns.
16 equal cells total.
No borders.
No labels.
No text.

ROW LAYOUT:
Row 1 = walking LEFT - CRITICAL:**must follow the 4-FRAME-WALK-RULES** 
Row 2 = walking RIGHT - CRITICAL:**must follow the 4-FRAME-WALK-RULES**
Row 3 = walking UP
Row 4 = walking DOWN

ANIMATION RULES:
- smooth simple 4-frame RPG walk cycle - CRITICAL:**must follow the 4-FRAME-WALK-RULES** 
- only arms and legs move 
- torso remains locked and stable
- head remains stable
- same body height in all 16 cells
- same body width in all 16 cells
- same head size in all 16 cells
- same clothing in all 16 cells
- feet aligned to the same ground line within each row
- character centered in every cell
- full body visible in every frame
- equal padding around sprite
- no limb crossing outside cells
- no scaling changes
- no anatomy drift
- no camera changes
- no perspective changes

CRITICAL:
The output must look like the same exact cute chibi RPG character copied across all frames, with only the walking pose changing.

CRITICAL:**must follow the 4-FRAME-WALK-RULES** 

BACKGROUND:
solid flat #FF00FF magenta only.
No shadows.
No gradients.
No props.
No text.`;

const CHARGEN_PAGE2_PROMPT = `You are given TWO reference images, in this order:
  1. The character PORTRAIT (close-up of face/upper body).
  2. PAGE 1 — the WALK-CYCLE sprite sheet for THIS SAME CHARACTER, generated
     in the previous step. It shows the character in 16 cells (4 directions ×
     4 walk frames) on a magenta background.

═══════════════════════════════════════════════════════
CHARACTER LOCK — match Page 1 EXACTLY
═══════════════════════════════════════════════════════

The character on this Page 2 sheet must be VISUALLY IDENTICAL to the
character on Page 1. This is a hard constraint. Specifically copy from Page 1:
  - exact head size and shape
  - exact facial features (eyes, nose, mouth, eyebrows, beard / markings)
  - exact hair colour, length, and style
  - exact body proportions (head : torso : leg ratio)
  - exact clothing: garment cuts, colours, palette, accessories
  - exact skin tone and outline weight
  - exact pixel-art style and shading

If you would draw the character even slightly differently than Page 1,
DO NOT — copy Page 1's character pixel-for-pixel and only change the pose.

STYLE TARGET (must also match Page 1):
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly
- chibi office-worker proportions
- large head
- compact small body
- cute simplified face
- clean dark outlines
- simple shading
- low-detail clothing
- readable 16-bit RPG overworld sprite
- not realistic
- not highly detailed
- not tall adult proportions
- not isometric
- not painterly

IMPORTANT:
Preserve the recognizable identity and species traits from the reference image while transforming the character into a humanoid office worker suitable for a top-down RPG game.

The character should:
- have cute chibi human-like body proportions
- wear the SAME casual office-worker clothes in every frame
- maintain the recognizable identity and silhouette from the reference
- keep the same head size, same face, same beard/markings/features, same clothing, same body scale

4-FRAME WALK LEFT AND WRITE CYCLE STRUCTURE:
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

CLOTHING LOCK:
Use the exact same outfit in every frame:
Do not change to shirt, pants, dress or shoes.

SHEET FORMAT:
Exactly 4 rows and 4 columns.
16 equal cells total.
No borders.
No labels.
No text.

ROW LAYOUT:
Row 1 = compact sleeping animation
Row 2 = standing drinking coffee animation
Row 3 = standing thinking animation
Row 4 = front-facing seated laptop working animation

FRAME LAYOUT:
Column 1 = start pose
Column 2 = mid pose
Column 3 = strongest pose
Column 4 = return pose

ROW 1 SLEEPING:
The character is sleeping in a compact seated or curled resting pose.
Keep the whole body compact and centered.
No sofa, no bed, no pillow, no blanket.
Subtle breathing only.
Do not add Z letters, text, symbols, bubbles, or icons.

ROW 2 DRINKING COFFEE:
The character remains STANDING UPRIGHT in all 4 frames.
Small coffee cup held close to the chest and mouth.
The cup must stay inside the same body silhouette width.
Do not extend the arm far outward.
Do not make the cup large.
Keep body height and width identical to the standing thinking row.

ROW 3 THINKING:
The character remains STANDING UPRIGHT in all 4 frames.
Subtle thinking gesture only, such as hand near chin.
Do not add thought bubbles, icons, dots, symbols, question marks, or text.
Keep body height and width identical to the coffee row.

ROW 4 LAPTOP WORK:
The character is sitting FRONT-FACING toward the camera on a simple small chair.
The laptop is centered directly in front of the torso, not to the left or right.
The laptop must fit completely inside the same sprite bounding box width as the standing rows.
The laptop must NOT extend outside the character’s normal shoulder-and-arm width.
The laptop must NOT make the sprite wider than the standing coffee or thinking sprites.
The laptop must be small, compact, and close to the body.
Hands stay close together on the keyboard.
Chair is small and mostly hidden behind the character.
Do not use side-facing laptop pose.
Do not use a desk.
Do not make a wide laptop.
Do not let the laptop touch or cross the cell edge.

STRICT WIDTH RULE:
For every cell in Row 4, the total visible width of character + chair + laptop must be equal to or narrower than the standing character width in Rows 2 and 3.
This is mandatory because the sprites will be cropped into equal-width game frames.

ANIMATION RULES:
- smooth simple looping office animations
- subtle movement only
- same body scale in all 16 cells
- same head size in all 16 cells
- same clothing in all 16 cells
- torso remains stable
- head remains stable
- no anatomy drift
- no changing proportions
- no scaling changes
- body centered in every cell
- equal padding around sprite
- no limb, cup, chair, laptop, or prop crossing outside cells
- no text or symbols anywhere

CRITICAL:
The output must look like the same exact cute chibi RPG character copied across all frames, with only the pose changing.
The laptop row must be compact and must not be wider than the standing rows.

4-FRAME WALK LEFT AND WRITE CYCLE STRUCTURE:
Frame 1: Contact (Left Leg Forward): The left foot strikes the ground while the right arm moves forward in opposition.
Frame 2: Passing (Middle): The arms and legs align in the center as the weight shifts forward.
Frame 3: Contact (Right Leg Forward): The right foot strikes the ground while the left arm moves forward.
Frame 4: Passing (Middle): The character returns to a centered position, completing the cycle before looping back to Frame 1.

BACKGROUND:
solid flat #FF00FF magenta only.
No shadows.
No gradients.
No text.
No labels.
No icons.
No speech bubbles.
No thought bubbles.
No Z letters.`;

const CHARGEN_LOCKED_PROMPT =
  "── PAGE 1 (walk cycle) ──\n" + CHARGEN_PAGE1_PROMPT + "\n\n── PAGE 2 (stances) ──\n" + CHARGEN_PAGE2_PROMPT;

const CHARGEN_EMOJI_SUGGESTIONS = [
  "🦁", "🐯", "🐱", "🐶", "🐼", "🦊", "🐻", "🐨", "🦄", "🐲", "🐢", "🦉",
  "🤖", "👽", "🧙", "🧝", "🧚", "🦸", "🥷", "👾", "🐰", "🐸", "🐧", "🦕",
  "🌟", "🔥", "⚡", "🌈", "🍎", "🌸", "☕", "📚", "🎮", "🎨", "🚀", "🎩",
];

// Sprite-strip poses (new-format names with legacy fallbacks) — mirrors
// renderSpriteStrip's STRIP_POSES.
const STRIP_POSES: { key: string; label: string; legacy?: boolean }[] = [
  { key: "walk_left_a", label: "walk ←" },
  { key: "walk_right_a", label: "walk →" },
  { key: "walk_up_a", label: "walk ↑" },
  { key: "walk_down_a", label: "walk ↓" },
  { key: "idle_down", label: "idle" },
  { key: "sleep", label: "sleep" },
  { key: "coffee", label: "coffee" },
  { key: "idle_thinking", label: "think" },
  { key: "work", label: "work" },
  { key: "idle", label: "idle", legacy: true },
  { key: "walk_a", label: "walk·a", legacy: true },
  { key: "wave", label: "wave", legacy: true },
];

// One entry of GET /api/avatar/models, narrowed to what this dialog needs.
interface AvatarEngine {
  id: string;
  provider: string;
  label: string;
  available: boolean;
  default?: boolean;
  reason?: string;
}

async function multipart(path: string, fd: FormData): Promise<Record<string, unknown>> {
  const res = await fetch(path, { method: "POST", credentials: "include", body: fd });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? String((data as { detail: unknown }).detail) : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return (data as Record<string, unknown>) || {};
}

async function del(path: string): Promise<void> {
  const res = await fetch(path, { method: "DELETE", credentials: "include" });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
}

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

export function AvatarEditor({
  agentName,
  avatarUrl,
  sprites,
  emoji,
  color,
  description,
  openaiImageGen,
  spritesPending,
  onSpritesPending,
  onChanged,
}: {
  agentName: string;
  avatarUrl?: string | null;
  sprites?: Record<string, string> | null;
  emoji?: string;
  color?: string | null;
  description?: string;
  openaiImageGen: boolean;
  spritesPending: boolean;
  onSpritesPending: (pending: boolean) => void;
  onChanged: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [genOpen, setGenOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmEmoji, setConfirmEmoji] = useState(false);
  const [confirmSprites, setConfirmSprites] = useState(false);

  const hasSprites = !!sprites && STRIP_POSES.some((p) => sprites[p.key]);

  async function onUpload(ev: React.ChangeEvent<HTMLInputElement>) {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) {
      toast.push(t("avatar.tooBig"), "error");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await multipart(`/api/agents/${encodeURIComponent(agentName)}/avatar`, fd);
      toast.push(t("avatar.updated"));
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("avatar.uploadFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function useEmoji() {
    setBusy(true);
    try {
      await del(`/api/agents/${encodeURIComponent(agentName)}/avatar`);
      toast.push(t("avatar.removed"));
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("avatar.clearFailed")), "error");
    } finally {
      setBusy(false);
      setConfirmEmoji(false);
    }
  }

  async function removeSprites() {
    setBusy(true);
    try {
      await del(`/api/agents/${encodeURIComponent(agentName)}/sprites`);
      toast.push(t("avatar.spritesRemoved"));
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("avatar.clearFailed")), "error");
    } finally {
      setBusy(false);
      setConfirmSprites(false);
    }
  }

  const enc = encodeURIComponent(agentName);

  return (
    <div className="card" style={{ padding: 16 }}>
      <strong style={{ fontSize: 13 }}>{t("avatar.title")}</strong>
      <div className="row" style={{ gap: 14, marginTop: 12, alignItems: "flex-start" }}>
        <div
          style={{
            width: 84,
            height: 84,
            borderRadius: "var(--r-md)",
            flexShrink: 0,
            display: "grid",
            placeItems: "center",
            fontSize: 38,
            overflow: "hidden",
            background: color ? `${color}30` : "var(--accent-gradient)",
            border: color ? `1px solid ${color}66` : "1px solid var(--border)",
          }}
        >
          {busy ? (
            <span className="spinner" />
          ) : avatarUrl ? (
            <img src={avatarUrl} alt={agentName} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          ) : (
            <span>{emoji || "🦁"}</span>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <button className="btn row" style={{ gap: 6, padding: "5px 10px", fontSize: 12 }} disabled={busy} onClick={() => fileRef.current?.click()}>
              <Upload size={13} strokeWidth={1.75} /> {t("avatar.upload")}
            </button>
            <button
              className="btn primary row"
              style={{ gap: 6, padding: "5px 10px", fontSize: 12, opacity: openaiImageGen ? 1 : 0.5 }}
              disabled={busy || !openaiImageGen}
              title={openaiImageGen ? undefined : t("avatar.genDisabledTip")}
              onClick={() => setGenOpen(true)}
            >
              <Sparkles size={13} strokeWidth={1.75} /> {t("avatar.generate")}
            </button>
            {avatarUrl && (
              <button className="btn ghost" style={{ padding: "5px 10px", fontSize: 12 }} disabled={busy} onClick={() => setConfirmEmoji(true)}>
                {t("avatar.useEmoji")}
              </button>
            )}
          </div>
          <p className="muted" style={{ fontSize: 11, margin: "8px 0 0", lineHeight: 1.5 }}>
            {t("avatar.hint")}{" "}
            {openaiImageGen ? (
              <>
                {t("avatar.hintGen")} <code>gpt-image-2.5-flare</code>.
              </>
            ) : (
              <span style={{ color: "var(--warn)" }}>{t("avatar.hintNoKey")}</span>
            )}
          </p>
        </div>
      </div>
      <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }} onChange={onUpload} />

      {/* sprite strip — real sprites win over a stale pending flag */}
      {hasSprites ? (
        <div style={{ marginTop: 14 }}>
          <SpriteStrip sprites={sprites!} />
          <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="btn ghost" style={{ padding: "4px 9px", fontSize: 12 }} onClick={() => setConfirmSprites(true)}>
              {t("avatar.removeSprites")}
            </button>
            <a className="btn ghost" style={{ padding: "4px 9px", fontSize: 12 }} href={`/api/agents/${enc}/sprites/motion`} target="_blank" rel="noopener noreferrer">
              {t("avatar.walkSheet")}
            </a>
            <a className="btn ghost" style={{ padding: "4px 9px", fontSize: 12 }} href={`/api/agents/${enc}/sprites/stance`} target="_blank" rel="noopener noreferrer">
              {t("avatar.stanceSheet")}
            </a>
          </div>
        </div>
      ) : spritesPending ? (
        <div style={{ marginTop: 14 }}>
          <SpriteStrip sprites={null} pending />
          <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            {t("avatar.spritesPending")}
          </p>
        </div>
      ) : null}

      {genOpen && (
        <GenerateModal
          agentName={agentName}
          defaultDescription={description || `${agentName}: a friendly Olano mascot`}
          onClose={() => setGenOpen(false)}
          onPending={onSpritesPending}
          onDone={onChanged}
        />
      )}
      {confirmEmoji && (
        <ConfirmModal
          title={t("avatar.useEmojiTitle")}
          message={t("avatar.useEmojiMsg")}
          confirmLabel={t("avatar.useEmoji")}
          busy={busy}
          onConfirm={() => void useEmoji()}
          onClose={() => setConfirmEmoji(false)}
        />
      )}
      {confirmSprites && (
        <ConfirmModal
          title={t("avatar.removeSpritesTitle")}
          message={t("avatar.removeSpritesMsg")}
          confirmLabel={t("btn.remove")}
          danger
          busy={busy}
          onConfirm={() => void removeSprites()}
          onClose={() => setConfirmSprites(false)}
        />
      )}
    </div>
  );
}

function SpriteStrip({ sprites, pending }: { sprites: Record<string, string> | null; pending?: boolean }) {
  const hasNew = !!(sprites?.walk_left_a || sprites?.walk_right_a);
  const cells = STRIP_POSES.filter((p) => !p.legacy || (!hasNew && !pending));
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {cells.map((p) => {
        const url = sprites?.[p.key];
        if (!pending && !url) return null;
        return (
          <div
            key={p.key}
            title={p.key}
            style={{
              width: 54,
              borderRadius: "var(--r-sm)",
              border: "1px solid var(--border)",
              background: "var(--surface-3)",
              padding: 4,
              textAlign: "center",
            }}
          >
            <div style={{ height: 48, display: "grid", placeItems: "center" }}>
              {pending ? (
                <span className="spinner" />
              ) : (
                <img src={url} alt={p.key} style={{ maxWidth: "100%", maxHeight: 48, imageRendering: "pixelated" }} />
              )}
            </div>
            <div className="muted" style={{ fontSize: 10, marginTop: 2 }}>
              {p.label}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function GenerateModal({
  agentName,
  defaultDescription,
  onClose,
  onPending,
  onDone,
}: {
  agentName: string;
  defaultDescription: string;
  onClose: () => void;
  onPending: (pending: boolean) => void;
  onDone: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [description, setDescription] = useState(defaultDescription);
  const [style, setStyle] = useState("");
  const [emoji, setEmoji] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [refUrl, setRefUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [engines, setEngines] = useState<AvatarEngine[]>([]);
  // "" = let the server choose (the marked default). Only set once the user picks.
  const [engine, setEngine] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get<{ models: AvatarEngine[] }>("/api/avatar/models")
      // The generators drive the OpenAI images API, which Gemini and Ollama do
      // not serve — listing them here would offer a choice that always fails.
      .then((d) => {
        if (live) setEngines((d.models || []).filter((m) => m.provider === "olano" || m.provider === "openai"));
      })
      .catch(() => {
        if (live) setEngines([]);
      });
    return () => {
      live = false;
    };
  }, []);
  const refInput = useRef<HTMLInputElement>(null);

  useEffect(() => () => {
    if (refUrl) URL.revokeObjectURL(refUrl);
  }, [refUrl]);

  function pickRef(ev: React.ChangeEvent<HTMLInputElement>) {
    const f = ev.target.files?.[0];
    ev.target.value = "";
    if (!f) return;
    if (f.size > 10 * 1024 * 1024) {
      toast.push(t("avatar.tooBig"), "error");
      return;
    }
    if (refUrl) URL.revokeObjectURL(refUrl);
    setRefFile(f);
    setRefUrl(URL.createObjectURL(f));
  }

  function clearRef() {
    if (refUrl) URL.revokeObjectURL(refUrl);
    setRefFile(null);
    setRefUrl(null);
  }

  async function generate() {
    setBusy(true);
    try {
      const fd = new FormData();
      if (description.trim()) fd.append("description", description.trim());
      if (style.trim()) fd.append("style", style.trim());
      if (emoji) fd.append("emoji", emoji);
      if (refFile) fd.append("file", refFile, refFile.name || "reference.png");
      if (engine) fd.append("model", engine);
      const data = await multipart(`/api/agents/${encodeURIComponent(agentName)}/character/generate`, fd);
      onPending(!!data.sprites_pending);
      if (data.sprites_pending) {
        toast.push(t("avatar.genPortraitReady"));
      } else {
        toast.push(t("avatar.genDone"));
      }
      onDone();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("avatar.genFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("avatar.genTitle")}
      subtitle={t("avatar.genSub")}
      onClose={onClose}
      width={640}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary row" style={{ gap: 6 }} onClick={() => void generate()} disabled={busy}>
            {busy ? <span className="spinner" /> : <Sparkles size={14} strokeWidth={1.75} />}
            {busy ? t("avatar.generating") : t("avatar.generate")}
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "150px 1fr", gap: 14 }}>
        <div>
          <label className="muted" style={{ fontSize: 12, display: "block", textAlign: "center", marginBottom: 6 }}>
            {t("avatar.reference")}
          </label>
          <button
            type="button"
            onClick={() => refInput.current?.click()}
            style={{
              width: "100%",
              aspectRatio: "1 / 1",
              borderRadius: "var(--r-md)",
              border: "1px dashed var(--border-strong)",
              background: "var(--surface-2)",
              cursor: "pointer",
              position: "relative",
              overflow: "hidden",
              color: "var(--text-3)",
              fontSize: 12,
            }}
          >
            {refUrl ? (
              <>
                <img src={refUrl} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                <span
                  onClick={(e) => {
                    e.stopPropagation();
                    clearRef();
                  }}
                  style={{ position: "absolute", top: 4, right: 4, background: "var(--surface)", borderRadius: "50%", width: 22, height: 22, display: "grid", placeItems: "center", border: "1px solid var(--border)" }}
                >
                  <X size={13} strokeWidth={2} />
                </span>
              </>
            ) : (
              <span>
                📷
                <br />
                {t("avatar.refDrop")}
              </span>
            )}
          </button>
          <input ref={refInput} type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }} onChange={pickRef} />
          <div className="muted" style={{ fontSize: 11, marginTop: 4, textAlign: "center", lineHeight: 1.4 }}>
            {t("avatar.refHint")}
          </div>
        </div>
        <div style={{ minWidth: 0 }}>
          {engines.length > 1 ? (
            <Field label={t("avatar.engine")} hint={t("avatar.engineHint")}>
              <select className="input" value={engine} onChange={(e) => setEngine(e.target.value)}>
                <option value="">{t("avatar.engineAuto")}</option>
                {engines.map((m) => (
                  <option key={m.id} value={m.id} disabled={!m.available}>
                    {m.label}
                    {m.available ? (m.default ? ` — ${t("avatar.engineDefaultTag")}` : "") : ` — ${m.reason || ""}`}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          <Field label={t("avatar.description")}>
            <textarea
              className="input"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("avatar.descPlaceholder")}
              style={{ resize: "vertical" }}
            />
          </Field>
          <Field label={t("avatar.styleNote")} hint={t("avatar.optional")}>
            <input className="input" value={style} onChange={(e) => setStyle(e.target.value)} placeholder={t("avatar.stylePlaceholder")} />
          </Field>
          <Field label={t("avatar.emojiVibe")} hint={t("avatar.optional")}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {CHARGEN_EMOJI_SUGGESTIONS.map((e) => (
                <button
                  key={e}
                  type="button"
                  onClick={() => setEmoji((cur) => (cur === e ? "" : e))}
                  style={{
                    width: 30,
                    height: 30,
                    fontSize: 16,
                    borderRadius: "var(--r-sm)",
                    cursor: "pointer",
                    background: emoji === e ? "color-mix(in srgb, var(--accent) 18%, var(--surface))" : "var(--surface-2)",
                    border: emoji === e ? "1px solid var(--accent)" : "1px solid var(--border)",
                  }}
                >
                  {e}
                </button>
              ))}
            </div>
          </Field>
        </div>
      </div>

      <details style={{ marginTop: 10 }}>
        <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-3)", userSelect: "none" }}>{t("avatar.lockedPrompt")}</summary>
        <pre
          style={{
            marginTop: 8,
            maxHeight: 220,
            overflow: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            color: "var(--text-3)",
            background: "var(--code-bg)",
            border: "1px solid var(--code-border)",
            borderRadius: "var(--r-sm)",
            padding: "10px 12px",
          }}
        >
          {CHARGEN_LOCKED_PROMPT}
        </pre>
      </details>
      <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>
        {t("avatar.genProcess")}
      </p>
    </Modal>
  );
}
