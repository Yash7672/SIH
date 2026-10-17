import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
// Base Leaflet CSS bundled from the `leaflet` npm dep so the map never depends
// on the unpkg CDN being reachable (blocked CDN => blank/broken map). Must load
// BEFORE the theme file, which only re-skins controls/popups laid out by it.
import "leaflet/dist/leaflet.css";
import "./leaflet-theme.css";
import { ThemeProvider } from "./hooks/useTheme";
import ErrorBoundary from "./components/ErrorBoundary";
import { BrowserRouter } from "react-router-dom";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* Outermost so a throw anywhere below still renders a readable message
        instead of an empty #root. It uses window.location, not router hooks,
        so it deliberately sits OUTSIDE the Router and keeps working if the
        router itself ever fails. */}
    <ErrorBoundary appName="Police Dashboard">
      <ThemeProvider>
        {/* The single Router for this app. App.tsx renders <Routes>, and every
            page, Link and useNavigate below it needs this context; without it
            React throws "useRoutes() may be used only in the context of a
            <Router> component" and the whole page is blank. */}
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ThemeProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
