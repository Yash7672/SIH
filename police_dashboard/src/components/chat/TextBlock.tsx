import { Fragment, type ReactNode } from "react";

/**
 * Render assistant text safely.
 *
 * The answer comes from a language model, so it is untrusted: it is NEVER passed
 * to dangerouslySetInnerHTML. Only two things are interpreted - line breaks and
 * `**bold**` - and everything else is emitted as literal text. A message
 * containing `<script>` renders the angle brackets on screen, which is the point.
 */
export function TextBlock({ text }: { text: string }) {
  return <>{text.split("\n").map((line, index) => renderLine(line, index))}</>;
}

function renderLine(line: string, key: number): ReactNode {
  if (!line) return <br key={key} />;

  // Split on **bold** while keeping the delimiters, so the pieces alternate.
  const parts = line.split(/(\*\*[^*]+\*\*)/g).filter((part) => part !== "");
  return (
    <Fragment key={key}>
      {parts.map((part, index) =>
        part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
          <strong key={index} className="font-semibold">
            {part.slice(2, -2)}
          </strong>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
      {key >= 0 ? <br /> : null}
    </Fragment>
  );
}
