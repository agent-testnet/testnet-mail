package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
)

var (
	domain     string
	container  string
	dockerAPI  string
	userRe     = regexp.MustCompile(`^[a-zA-Z0-9._-]{1,64}$`)
	httpClient = &http.Client{Timeout: 30 * time.Second}
)

func main() {
	domain = getEnv("MAIL_DOMAIN", "gmail.com")
	container = getEnv("MAIL_CONTAINER", "mailserver")
	dockerAPI = getEnv("DOCKER_API", "http://docker-proxy:2375")

	http.HandleFunc("/signup", handler)
	log.Printf("signup-api listening on :8081 (domain=%s, container=%s, docker=%s)", domain, container, dockerAPI)
	log.Fatal(http.ListenAndServe(":8081", nil))
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func handler(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		token := setCSRFCookie(w)
		showForm(w, "", "", token)
	case http.MethodPost:
		if !validCSRF(r) {
			http.Error(w, "Invalid request. Please go back and try again.", http.StatusForbidden)
			return
		}
		handleSignup(w, r)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

// CSRF: double-submit cookie pattern (stateless, no server-side session needed)
func setCSRFCookie(w http.ResponseWriter) string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		log.Printf("WARN: crypto/rand failed: %v", err)
	}
	token := hex.EncodeToString(b)
	http.SetCookie(w, &http.Cookie{
		Name:     "_csrf",
		Value:    token,
		Path:     "/signup",
		HttpOnly: true,
		Secure:   true,
		SameSite: http.SameSiteStrictMode,
		MaxAge:   3600,
	})
	return token
}

func validCSRF(r *http.Request) bool {
	cookie, err := r.Cookie("_csrf")
	if err != nil || cookie.Value == "" {
		return false
	}
	return cookie.Value == r.FormValue("_csrf")
}

func handleSignup(w http.ResponseWriter, r *http.Request) {
	token := setCSRFCookie(w)
	username := strings.TrimSpace(r.FormValue("username"))
	password := r.FormValue("password")
	confirm := r.FormValue("confirm")

	if username == "" || password == "" {
		showForm(w, "Username and password are required.", username, token)
		return
	}
	if !userRe.MatchString(username) {
		showForm(w, "Username may only contain letters, numbers, dots, hyphens, and underscores.", username, token)
		return
	}
	if len(password) < 6 {
		showForm(w, "Password must be at least 6 characters.", username, token)
		return
	}
	if password != confirm {
		showForm(w, "Passwords do not match.", username, token)
		return
	}

	email := username + "@" + domain

	if accountExists(email) {
		showForm(w, fmt.Sprintf("Account %s already exists.", email), username, token)
		return
	}

	output, exitCode, err := dockerExec([]string{"setup", "email", "add", email, password})
	if err != nil || exitCode != 0 {
		log.Printf("account creation failed for %s: exit=%d err=%v output=%s", email, exitCode, err, output)
		showForm(w, "Account creation failed. Please try again.", username, token)
		return
	}

	log.Printf("account created: %s", email)
	showSuccess(w, email)
}

func accountExists(email string) bool {
	output, exitCode, err := dockerExec([]string{"setup", "email", "list"})
	if err != nil || exitCode != 0 {
		return false
	}
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		// docker-mailserver outputs: "* user@domain ( 0 / ~ ) [0%]"
		if len(fields) >= 2 && fields[1] == email {
			return true
		}
	}
	return false
}

// Docker Engine API — runs commands inside the mailserver container via the
// socket proxy. Passwords travel in the JSON request body, never in a process
// argument list (invisible to `ps aux`).
func dockerExec(cmd []string) (string, int, error) {
	createBody, _ := json.Marshal(map[string]interface{}{
		"Cmd":          cmd,
		"AttachStdout": true,
		"AttachStderr": true,
		"Tty":          true,
	})

	createResp, err := httpClient.Post(
		dockerAPI+"/containers/"+container+"/exec",
		"application/json",
		bytes.NewReader(createBody),
	)
	if err != nil {
		return "", -1, fmt.Errorf("exec create: %w", err)
	}
	defer createResp.Body.Close()

	if createResp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(createResp.Body)
		return "", -1, fmt.Errorf("exec create: status %d: %s", createResp.StatusCode, body)
	}

	var execCreate struct{ Id string }
	if err := json.NewDecoder(createResp.Body).Decode(&execCreate); err != nil {
		return "", -1, fmt.Errorf("exec create decode: %w", err)
	}

	startBody, _ := json.Marshal(map[string]interface{}{
		"Detach": false,
		"Tty":    true,
	})

	startResp, err := httpClient.Post(
		dockerAPI+"/exec/"+execCreate.Id+"/start",
		"application/json",
		bytes.NewReader(startBody),
	)
	if err != nil {
		return "", -1, fmt.Errorf("exec start: %w", err)
	}
	defer startResp.Body.Close()

	output, _ := io.ReadAll(startResp.Body)

	inspectResp, err := httpClient.Get(dockerAPI + "/exec/" + execCreate.Id + "/json")
	if err != nil {
		return string(output), -1, fmt.Errorf("exec inspect: %w", err)
	}
	defer inspectResp.Body.Close()

	var execInspect struct{ ExitCode int }
	json.NewDecoder(inspectResp.Body).Decode(&execInspect)

	return string(output), execInspect.ExitCode, nil
}

func showForm(w http.ResponseWriter, errMsg, username, csrfToken string) {
	errorHTML := ""
	if errMsg != "" {
		errorHTML = fmt.Sprintf(`<div class="error-msg">%s</div>`, html.EscapeString(errMsg))
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, formPage, html.EscapeString(domain), errorHTML, html.EscapeString(username), html.EscapeString(csrfToken))
}

func showSuccess(w http.ResponseWriter, email string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, successPage, html.EscapeString(email))
}

var formPage = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Create Account - Gmail</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
       background: #f4f5f6; display: flex; justify-content: center; align-items: center;
       min-height: 100vh; }
.card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        padding: 2.5rem; width: 100%%; max-width: 420px; }
h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 0.5rem; color: #333; }
.subtitle { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
label { display: block; font-size: 0.85rem; color: #555; margin-bottom: 0.3rem; font-weight: 500; }
input[type="text"], input[type="password"] {
  width: 100%%; padding: 0.6rem 0.75rem; border: 1px solid #ccc; border-radius: 4px;
  font-size: 0.95rem; margin-bottom: 1rem; }
input:focus { outline: none; border-color: #4285f4; box-shadow: 0 0 0 2px rgba(66,133,244,0.2); }
.email-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; }
.email-row input { flex: 1; margin-bottom: 0; }
.domain-suffix { color: #666; font-size: 0.95rem; white-space: nowrap; }
button { width: 100%%; padding: 0.7rem; background: #4285f4; color: #fff; border: none;
         border-radius: 4px; font-size: 1rem; cursor: pointer; font-weight: 500; }
button:hover { background: #3367d6; }
.error-msg { background: #fce8e6; color: #c5221f; padding: 0.6rem 0.75rem; border-radius: 4px;
             font-size: 0.85rem; margin-bottom: 1rem; }
.login-link { text-align: center; margin-top: 1.2rem; }
.login-link a { color: #4285f4; text-decoration: none; font-size: 0.9rem; }
.login-link a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="card">
  <h1>Create Account</h1>
  <p class="subtitle">Create a new email account on %[1]s</p>
  %[2]s
  <form method="POST" action="/signup">
    <input type="hidden" name="_csrf" value="%[4]s">
    <label for="username">Username</label>
    <div class="email-row">
      <input type="text" id="username" name="username" value="%[3]s"
             placeholder="yourname" required pattern="[a-zA-Z0-9._-]+"
             title="Letters, numbers, dots, hyphens, underscores">
      <span class="domain-suffix">@%[1]s</span>
    </div>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required minlength="6"
           placeholder="At least 6 characters">
    <label for="confirm">Confirm password</label>
    <input type="password" id="confirm" name="confirm" required minlength="6"
           placeholder="Repeat your password">
    <button type="submit">Create Account</button>
  </form>
  <div class="login-link">
    <a href="/">Already have an account? Sign in</a>
  </div>
</div>
</body>
</html>`

var successPage = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account Created - Gmail</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
       background: #f4f5f6; display: flex; justify-content: center; align-items: center;
       min-height: 100vh; }
.card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        padding: 2.5rem; width: 100%%; max-width: 420px; text-align: center; }
h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 0.75rem; color: #333; }
.email { font-size: 1.1rem; color: #4285f4; font-weight: 500; margin-bottom: 1.5rem; }
a.btn { display: inline-block; padding: 0.7rem 2rem; background: #4285f4; color: #fff;
        border-radius: 4px; text-decoration: none; font-size: 1rem; font-weight: 500; }
a.btn:hover { background: #3367d6; }
</style>
</head>
<body>
<div class="card">
  <h1>Account Created</h1>
  <p class="email">%s</p>
  <a class="btn" href="/">Sign in</a>
</div>
</body>
</html>`
