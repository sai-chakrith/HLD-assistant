document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  const message = document.getElementById("message");
  try {
    const response = await fetch("/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: document.getElementById("access-token").value }) });
    if (!response.ok) { message.textContent = (await response.json()).detail || "Sign-in failed."; return; }
    location.assign("/");
  } catch { message.textContent = "The server did not respond."; }
  finally { button.disabled = false; }
});
