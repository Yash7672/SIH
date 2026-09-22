import { Outlet, Link, useNavigate } from "react-router-dom";

export default function Layout() {
  const nav = useNavigate();
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");

  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-white border-b border-slate-200">
        <div className="mx-auto max-w-5xl px-4 py-3 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2 font-bold text-lg text-brand-700">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-white text-sm">R</span>
            RAKSHAK
            <span className="ml-2 text-xs font-medium text-slate-400">Citizen Portal</span>
          </Link>
          <nav className="flex items-center gap-4 text-sm">
            <Link to="/" className="text-slate-600 hover:text-brand-600">Dashboard</Link>
            <Link to="/complaints/new" className="text-slate-600 hover:text-brand-600">File Complaint</Link>
            <Link to="/complaints" className="text-slate-600 hover:text-brand-600">My Complaints</Link>
            <button
              onClick={() => {
                localStorage.clear();
                nav("/login");
              }}
              className="rounded-md bg-slate-100 px-3 py-1.5 text-slate-700 hover:bg-slate-200"
            >
              Logout{user ? ` (${user.name.split(" ")[0]})` : ""}
            </button>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl w-full px-4 py-6 flex-1">
        <Outlet />
      </main>
      <footer className="border-t border-slate-200 py-4 text-center text-xs text-slate-400">
        RAKSHAK — Privacy-First Crowdsourced ANPR · Hackathon Demo
      </footer>
    </div>
  );
}
