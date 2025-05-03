
import React from "react";
import ReactDOM from "react-dom/client";
import { BlueprintProvider } from "@blueprintjs/core";
import App from "./App";

const params = new URLSearchParams(window.location.search);
var canvas_path = '';
if (params.has('path')) {
  canvas_path = params.get('path') ?? '';
  console.log('Path:', canvas_path);
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BlueprintProvider>
        <App path={canvas_path} />
    </BlueprintProvider>
  </React.StrictMode>
);

