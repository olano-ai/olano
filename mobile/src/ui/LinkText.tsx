/**
 * Plain text with its web addresses as links (chat/links.ts): a person's own
 * message, a note from the deployment. React draws every part, so nothing in
 * the text is ever read as markup.
 */

import { memo, useMemo } from "react";

import { splitLinks } from "../chat/links.ts";
import { openLink } from "./Markdown.tsx";

export const LinkText = memo(function LinkText({ text }: { text: string }) {
  const parts = useMemo(() => splitLinks(text), [text]);
  return (
    <>
      {parts.map((part, i) =>
        part.href ? (
          <a
            key={i}
            href={part.href}
            className="olano-link"
            onClick={(e) => {
              e.preventDefault();
              void openLink(part.href!);
            }}
          >
            {part.text}
          </a>
        ) : (
          part.text
        ),
      )}
    </>
  );
});
