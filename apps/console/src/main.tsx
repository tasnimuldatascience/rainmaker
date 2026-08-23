import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Register the service worker so the app shell boots from disk with no network. Registered
// after render rather than before: the worker is what makes the NEXT cold start work, and
// blocking first paint on it would trade a real cost for no benefit.
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .catch((err) => console.warn("service worker registration failed", err));
  });
}
