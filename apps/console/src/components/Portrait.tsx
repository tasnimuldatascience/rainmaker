/**
 * Nadia's face: a photoreal portrait that moves to the audio actually coming out.
 *
 * HER MOUTH IS GENERATED, NOT WARPED. A canvas sits over the lower half of the photograph and
 * shows the frame Wav2Lip produced for whatever audio is playing at this instant — real
 * lip-sync from a real model on the local GPU, not a still stretched on a sine wave. See
 * `calls/lipsync.py` for how it is fast enough to be inside a conversation.
 *
 * WHEN THERE ARE NO FRAMES the canvas is empty and the photograph shows through untouched: no
 * checkpoint installed, the browser voice speaking, or simply the first fraction of a second
 * before that clip's frames arrive. Frames follow their audio rather than delaying it, so the
 * mouth occasionally joins a beat late. Nothing is faked to cover the gap — a mouth inventing
 * movement it has no audio for is the uncanny thing this avoids.
 *
 * THE REST OF THE MOTION IS STILL REAL AND STILL NOT FACIAL: the bloom tracks the RMS of the
 * playing buffer and the drift is a camera, not a person.
 *
 * SIXTY FRAMES A SECOND, ZERO RE-RENDERS. Loudness changes every frame; putting it in React
 * state would re-render the call view sixty times a second to move one element two pixels.
 * The animation frame writes CSS custom properties on the wrapper and React never hears about
 * it.
 */

import { useEffect, useRef, useState } from "react";

import { Avatar } from "./Avatar";

/**
 * The crop Wav2Lip was handed, as fractions of the PORTRAIT IMAGE: left, top, right, bottom.
 * Must equal `FACE_FRACTIONS` in `calls/lipsync.py`; `lipsync.test.ts` asserts it.
 */
const MOUTH_BOX = [0.3, 0.36, 0.86, 0.92] as const;

/**
 * Where that crop lands on screen, given `object-fit: cover`.
 *
 * THIS IS NOT A PERCENTAGE OF THE BOX, which is how it was written first and why the generated
 * mouth appeared over her eye. The portrait is square and the window it sits in is not, so
 * `cover` scales the image up and crops the overflow — a fraction of the IMAGE and a fraction of
 * the CONTAINER are different rectangles, and the difference is exactly the crop.
 */
function coverRect(container: DOMRect, naturalWidth: number, naturalHeight: number) {
  const scale = Math.max(container.width / naturalWidth, container.height / naturalHeight);
  const shownWidth = naturalWidth * scale;
  const shownHeight = naturalHeight * scale;
  const offsetX = (container.width - shownWidth) / 2;
  const offsetY = (container.height - shownHeight) / 2;
  const [left, top, right, bottom] = MOUTH_BOX;
  return {
    left: offsetX + left * shownWidth,
    top: offsetY + top * shownHeight,
    width: (right - left) * shownWidth,
    height: (bottom - top) * shownHeight,
  };
}

/**
 * Fade the patch out at its edges.
 *
 * The generated crop is pasted onto the photograph, and a hard rectangular seam across someone's
 * cheek is more distracting than a still mouth ever was. `destination-in` keeps the pixels where
 * the gradient is opaque, so the middle of the mouth is the model's and the border melts back
 * into the original face.
 */
function feather(context: CanvasRenderingContext2D, width: number, height: number): void {
  const previous = context.globalCompositeOperation;
  context.globalCompositeOperation = "destination-in";

  // CENTRED ON THE MOUTH, AND TIGHT AROUND IT. Wav2Lip is handed a crop that runs from the
  // eyebrows to the chin and hands back a re-rendered copy of all of it — a mouth it generated,
  // and a nose, cheeks and jaw it merely redrew, slightly softer and a hair different every
  // frame. Keeping that whole rectangle meant the middle of her face very slightly swam from
  // frame to frame, and a face that swims is exactly the "cheap" tell: nobody can name it, and
  // everybody sees it.
  //
  // So the mask is now an ellipse around the mouth rather than a fade across the crop. The nose
  // ridge, the cheekbones and the eyes are the untouched photograph in every frame, and only
  // what the model has something to say about is the model's.
  const centreX = width * 0.5;
  const centreY = height * 0.72;

  // Wider than tall: a mouth is an ellipse, and a circle big enough to cover the corners of the
  // lips reaches the jawline and the base of the nose.
  const stretch = 1.3;

  context.save();
  context.translate(centreX, centreY);
  context.scale(stretch, 1);
  context.translate(-centreX, -centreY);

  // A THIRD OF THE CROP, NOT ALL OF IT. The old radius was half the larger dimension, which
  // reached the eyes.
  const radius = height * 0.34;
  const gradient = context.createRadialGradient(
    centreX, centreY, radius * 0.5,
    centreX, centreY, radius,
  );
  gradient.addColorStop(0, "rgba(0,0,0,1)");
  gradient.addColorStop(0.62, "rgba(0,0,0,0.97)");
  gradient.addColorStop(0.84, "rgba(0,0,0,0.45)");
  gradient.addColorStop(1, "rgba(0,0,0,0)");
  context.fillStyle = gradient;
  // Generous, because the context is scaled and the rectangle has to still cover the canvas.
  context.fillRect(-width, -height, width * 3, height * 3);
  context.restore();

  context.globalCompositeOperation = previous;
}

/**
 * How much of the patch is worth keeping, as a fraction of its height.
 *
 * The generated crop is upscaled from 96x96, so it is softer than the photograph it lands on.
 * Drawing less of it means less of that softness is visible, and the seam has less distance to
 * travel before it is hidden by the mask above.
 */
const PATCH_KEEP = { top: 0.28, bottom: 1 } as const;

/*
 * THE DRAWN REGION HAS TO OUTREACH THE MASK, or its top edge becomes a visible line.
 *
 * The mask is an ellipse centred at 0.72 of the patch with a radius of 0.34, so it still carries
 * roughly a third of its opacity at 0.38 — and the patch was only drawn from 0.42 downwards. The
 * result was a faint horizontal seam across her cheeks: not the mask failing, the mask working
 * correctly on pixels that stopped existing a few rows too early. Starting at 0.28 puts the edge
 * of the image comfortably outside anything the gradient still shows.
 */

export interface PortraitProps {
  /** Reads the current output loudness, 0..1. Polled, never pushed. */
  level: () => number;
  /** The generated mouth for this instant, or null. Polled on the same frame as `level`. */
  mouth?: () => ImageBitmap | null;
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
  mouth,
  speaking,
  listening = false,
  size = 190,
  fill = false,
  src = "/agent/nadia.jpg",
}: PortraitProps) {
  const wrapper = useRef<HTMLDivElement | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const image = useRef<HTMLImageElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (failed) return;
    let frame = 0;
    // A slow lissajous drift so the frame is never perfectly still. Two primes so the loop is
    // long enough that nobody sees it repeat.
    const start = performance.now();
    // Where the micro-settle is heading, and where it has got to. See below.
    let settleX = 0;
    let settleY = 0;

    const tick = () => {
      const node = wrapper.current;
      if (node) {
        const now = (performance.now() - start) / 1000;
        const loud = speaking ? level() : 0;

        // The camera, not the face: a fraction of a percent of scale and a pixel of drift.
        const driftX = Math.sin(now / 7.3) * 1.4;
        const driftY = Math.cos(now / 11.7) * 1.1;
        const breathe = 1 + Math.sin(now / 4.1) * 0.004;

        // A MICRO-SETTLE, WHICH IS THE PART A STILL PHOTOGRAPH CANNOT DO. A sine drift is
        // perfectly smooth and perfectly periodic, and a face that moves that evenly reads as a
        // picture on a slow pan. People make small, irregular adjustments and then hold — so
        // every few seconds this picks a new sub-pixel offset and eases toward it, and the
        // easing means it is arriving rather than sliding. It is a millimetre of movement; the
        // point is that it is not on a metronome.
        //
        // Deterministic from the clock rather than random, so two frames rendered at the same
        // instant agree and nothing jitters when the tab is throttled and resumes.
        const bucket = Math.floor(now / 2.7);
        const noise = (seed: number) => {
          const value = Math.sin(seed * 127.1 + 311.7) * 43758.5453;
          return (value - Math.floor(value)) * 2 - 1;
        };
        settleX += (noise(bucket) * 1.1 - settleX) * 0.02;
        settleY += (noise(bucket + 99) * 0.8 - settleY) * 0.02;

        node.style.setProperty("--drift-x", `${(driftX + settleX).toFixed(2)}px`);
        node.style.setProperty("--drift-y", `${(driftY + settleY).toFixed(2)}px`);
        node.style.setProperty("--scale", `${(breathe + loud * 0.012).toFixed(4)}`);
        node.style.setProperty("--loud", loud.toFixed(3));

        const surface = canvas.current;
        const photo = image.current;
        const bitmap = mouth?.() ?? null;
        if (surface && photo) {
          if (!bitmap) {
            surface.style.opacity = "0";
          } else {
            const rect = coverRect(
              node.getBoundingClientRect(),
              photo.naturalWidth || 512,
              photo.naturalHeight || 512,
            );
            // Written only when it actually changes: touching layout every frame on an element
            // that is also being transformed is how a smooth face starts stuttering.
            if (Math.abs(rect.width - surface.width) > 0.5) {
              surface.style.left = `${rect.left}px`;
              surface.style.top = `${rect.top}px`;
              surface.style.width = `${rect.width}px`;
              surface.style.height = `${rect.height}px`;
              surface.width = Math.round(rect.width);
              surface.height = Math.round(rect.height);
            }
            const context = surface.getContext("2d");
            if (context) {
              context.clearRect(0, 0, surface.width, surface.height);
              // Wav2Lip generates at 96x96; the browser is the right place to scale it up,
              // which is what the reference implementation does too.
              //
              // ONLY THE LOWER PART OF THE PATCH IS DRAWN. The top of it is the model's redraw
              // of pixels that were already correct and already sharper in the photograph.
              const sourceTop = bitmap.height * PATCH_KEEP.top;
              const sourceHeight = bitmap.height * (PATCH_KEEP.bottom - PATCH_KEEP.top);
              const destTop = surface.height * PATCH_KEEP.top;
              // THE PATCH IS UPSCALED FROM 96x96 AND THE PHOTOGRAPH IS NOT, so it lands slightly
              // flatter and slightly paler than the face around it. A small contrast and
              // saturation nudge is not sharpening — it cannot put back detail that was never
              // generated — but it stops the mouth reading as a duller cut-out of a brighter
              // face, which is most of what "pasted on" actually looks like.
              context.filter = "contrast(1.06) saturate(1.05)";
              context.drawImage(
                bitmap,
                0, sourceTop, bitmap.width, sourceHeight,
                0, destTop, surface.width, surface.height - destTop,
              );
              context.filter = "none";
              feather(context, surface.width, surface.height);
              surface.style.opacity = "1";
            }
          }
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [level, mouth, speaking, failed]);

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
        ref={image}
        className="portrait-img"
        src={src}
        alt="Nadia, an AI account executive. A synthetic portrait; not a real person."
        draggable={false}
        onError={() => setFailed(true)}
      />
      {/* The generated mouth, over the lower half of the face. The rectangle matches
          `FACE_FRACTIONS` in calls/lipsync.py — the same crop the model was handed, so the
          patch lands exactly where it was cut from. Two numbers in two languages that have to
          agree; `lipsync.test.ts` asserts they do. */}
      <canvas className="portrait-mouth" ref={canvas} aria-hidden />

      {/* Painted over the photo rather than around it, so "she is talking" is legible at pip
          size where a thin outline would disappear. */}
      <span className="portrait-bloom" aria-hidden />
      <span className="portrait-ring" aria-hidden />
    </div>
  );
}
