import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { PairingGate, watchForSignOut } from "./components/Pairing";
import "./desk.css";

watchForSignOut();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <PairingGate>
      <App />
    </PairingGate>
  </StrictMode>,
);
