import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import WidgetApp from "./WidgetApp";
import "./styles.css";

const isWidget = new URLSearchParams(window.location.search).get("widget") === "1";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {isWidget ? <WidgetApp /> : <App />}
  </React.StrictMode>
);
