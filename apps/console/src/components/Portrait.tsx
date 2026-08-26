/**
 * Liv's face: a photoreal portrait that moves to the audio actually coming out.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO IS FAKE A MOUTH. Warping the lips of a still photograph
 * is the single fastest way to make a real-looking face look dead — the uncanny valley is
 * steepest exactly where a nearly-right face moves slightly wrong, and a 512-pixel still has no
 * information about what is behind the lips to move them with. A photoreal face that holds
 * still and is lit as though it is speaking reads as a video call with a frozen frame. A
 * photoreal face whose mouth stretches on a sine wave reads as a corpse.
 *
 * SO THE MOTION HERE IS ALL REAL AND NONE OF IT IS FACIAL: the presence ring and the light
 * bloom are driven by the RMS of the audio buffer that is playing at this instant, and the slow
 * drift is a camera, not a person. Nothing claims to be lip movement, so nothing is wrong.
 *
 * Genuine lip-sync is a provider swap, not a rewrite — see `calls/avatar.py`. With a streaming
 * avatar key set, the same slot renders a real talking head and this component steps aside.
 *
 * SIXTY FRAMES A SECOND, ZERO RE-RENDERS. Loudness changes every frame; putting it in React
 * state would re-render the call view sixty times a second to move one element two pixels.
 * The animation frame writes CSS custom properties on the wrapper and React never hears about
 * it.
 */

import { useEffect, useRef, useState } from "react";

import { Avatar } from "./Avatar";

export interface PortraitProps {
  /** Reads the current output loudness, 0..1. Polled, never pushed. */
  level: () => number;
  speaking: boolean;
  listening?: boolean;
  size?: number;
  /**
   * Fill the parent instead of taking a fixed size.
   *
   * An inline `width` beats a stylesheet, so a 168px square inside the 4:3 floating window left
   * a dark bar down each side. When the parent defines the frame, the portrait must not.
   */
  fill?: boolean;
  /** Where the portrait lives. Swapped for another face by replacing this file. */
  src?: string;
}

export function Portrait({
  level,
  speaking,
  listening = false,
  size = 190,
  fill = false,
  src = "/agent/liv.jpg",
}: PortraitProps) {
  const wrapper = useRef<HTMLDivElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (failed) return;
    let frame = 0;
    // A slow lissajous drift so the frame is never perfectly still. Two primes so the loop is
    // long enough that nobody sees it repeat.
    const start = performance.now();

    const tick = () => {
      const node = wrapper.current;
      if (node) {
        const now = (performance.now() - start) / 1000;
        const loud = speaking ? level() : 0;

        // The camera, not the face: a fraction of a percent of scale and a pixel of drift.
        const driftX = Math.sin(now / 7.3) * 1.4;
        const driftY = Math.cos(now / 11.7) * 1.1;
        const breathe = 1 + Math.sin(now / 4.1) * 0.004;

        node.style.setProperty("--drift-x", `${driftX.toFixed(2)}px`);
        node.style.setProperty("--drift-y", `${driftY.toFixed(2)}px`);
        node.style.setProperty("--scale", `${(breathe + loud * 0.012).toFixed(4)}`);
        node.style.setProperty("--loud", loud.toFixed(3));
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [level, speaking, failed]);

  if (failed) {
    // The portrait is a static asset and static assets go missing — a bad deploy, a stripped
    // build, someone swapping the face and mistyping the name. The vector rig is the fallback
    // rather than a broken-image icon: it is worse-looking and fully working, which is the
    // right way round, and it keeps a well-built component in use instead of orphaned.
    return <Avatar speaking={speaking} listening={listening} size={size} />;
  }

  return (
    <div
      ref={wrapper}
      className="portrait"
      data-speaking={speaking}
      data-listening={listening}
      style={fill ? undefined : { inlineSize: size, blockSize: size }}
    >
      <img
        className="portrait-img"
        src={src}
        alt="Liv, an AI account executive. A synthetic portrait; not a real person."
        draggable={false}
        onError={() => setFailed(true)}
      />
      {/* Painted over the photo rather than around it, so "she is talking" is legible at pip
          size where a thin outline would disappear. */}
      <span className="portrait-bloom" aria-hidden />
      <span className="portrait-ring" aria-hidden />
    </div>
  );
}
