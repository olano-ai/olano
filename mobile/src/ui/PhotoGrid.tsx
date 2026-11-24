/**
 * A message's photos, laid out like the platform's own messages: one photo at
 * its own shape, two side by side, three as one large and two stacked, four
 * or more as a grid whose last tile counts the rest. Tap one to see it full
 * screen. A photo still on its way to the deployment shows a spinner.
 */

import { IonSpinner } from "@ionic/react";
import { useRef, useState } from "react";

import { photoLayout, singlePhotoBox, type MessagePhoto } from "../chat/photos.ts";
import { useMediaSrc } from "./mediaCache.ts";
import { openPhotoViewer } from "./PhotoViewer.tsx";

const SINGLE_MAX_WIDTH = 248;
const SINGLE_MAX_HEIGHT = 320;

function Tile({
  photo,
  photos,
  index,
  more,
  single,
}: {
  photo: MessagePhoto;
  photos: MessagePhoto[];
  index: number;
  more: number;
  single: boolean;
}) {
  const src = useMediaSrc(photo);
  const ref = useRef<HTMLButtonElement>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(
    photo.width && photo.height ? { width: photo.width, height: photo.height } : null,
  );
  const box = single ? singlePhotoBox(size?.width, size?.height, SINGLE_MAX_WIDTH, SINGLE_MAX_HEIGHT) : null;
  const label = more ? `${photo.name}, and ${more} more` : photo.name;
  return (
    <button
      ref={ref}
      type="button"
      className={src ? "olano-photo" : "olano-photo loading"}
      style={box ? { width: box.width, height: box.height } : undefined}
      aria-label={`Open photo ${index + 1} of ${photos.length}: ${label}`}
      disabled={!src}
      onClick={() => openPhotoViewer(photos, index, ref.current)}
    >
      {src && (
        <img
          src={src}
          alt=""
          draggable={false}
          onLoad={(e) => {
            const img = e.currentTarget;
            if (!size && img.naturalWidth) setSize({ width: img.naturalWidth, height: img.naturalHeight });
          }}
        />
      )}
      {more > 0 && <span className="olano-photo-more">+{more}</span>}
    </button>
  );
}

export function PhotoGrid({ photos, sending }: { photos: MessagePhoto[]; sending?: boolean }) {
  if (!photos.length) return null;
  const layout = photoLayout(photos.length);
  const shown = photos.slice(0, layout.shown);
  return (
    <div className={`olano-photos ${layout.kind}${sending ? " sending" : ""}`}>
      {shown.map((photo, i) => (
        <Tile
          key={photo.key}
          photo={photo}
          photos={photos}
          index={i}
          single={layout.kind === "single"}
          more={i === shown.length - 1 ? layout.more : 0}
        />
      ))}
      {sending && (
        <span className="olano-photos-sending" aria-label="Sending">
          <IonSpinner name="crescent" />
        </span>
      )}
    </div>
  );
}
