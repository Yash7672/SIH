import type { ReactNode } from "react";

/**
 * Faint shield mark drawn above the watermark word. Inline SVG so there is
 * no image request and no extra dependency.
 */
function ShieldMark() {
  return (
    <svg
      className="page-watermark__shield"
      viewBox="0 0 24 28"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M12 1.5 2.5 5.2v8.1c0 6.1 4 11.4 9.5 13.2 5.5-1.8 9.5-7.1 9.5-13.2V5.2L12 1.5Z"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path d="M12 8.4 7.6 10v3.4c0 2.7 1.8 5 4.4 5.8 2.6-.8 4.4-3.1 4.4-5.8V10L12 8.4Z" fill="currentColor" />
    </svg>
  );
}

/**
 * Centred, low-opacity "POLICE" watermark.
 *
 * Fixed to the viewport, sits behind every other layer, never receives
 * pointer events and is hidden on print and on very short viewports.
 * The word is a prop so it can be changed in one place.
 */
export function Watermark({ text = "POLICE", shield = true }: { text?: string; shield?: boolean }) {
  return (
    <div className="page-watermark" aria-hidden="true">
      <div>
        {shield ? <ShieldMark /> : null}
        <span className="page-watermark__text">{text}</span>
      </div>
    </div>
  );
}

/**
 * The fixed dot-grid + diagonal texture + soft glow layer that sits behind
 * the whole app. Pure CSS (see .page-decor in index.css): no canvas, no JS,
 * no animation and no re-render on scroll.
 */
export function PageBackground({
  children,
  watermark = "POLICE",
}: {
  children?: ReactNode;
  watermark?: string | false;
}) {
  return (
    <>
      <div className="page-decor" aria-hidden="true" />
      {watermark ? <Watermark text={watermark} /> : null}
      {children}
    </>
  );
}

export default PageBackground;
