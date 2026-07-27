import tkinter as tk

def on_tap(event):
    print(f"\n---> [SUCCESS] TAP DETECTED at X:{event.x} Y:{event.y}! <---", flush=True)
    canvas.itemconfig(text_id, text="SCREEN TAPPED!", fill="#00FF00")
    root.after(1000, lambda: canvas.itemconfig(text_id, text="Tap anywhere...", fill="white"))

root = tk.Tk()
root.attributes('-fullscreen', True)
root.configure(bg="black")

canvas = tk.Canvas(root, bg="black", highlightthickness=0)
canvas.pack(fill="both", expand=True)

text_id = canvas.create_text(400, 240, text="Tap anywhere...", fill="white", font=("Helvetica", 24, "bold"))

# Bind the click event to everything
root.bind("<Button-1>", on_tap)
canvas.bind("<Button-1>", on_tap)
root.bind("<Escape>", lambda e: root.destroy()) # Press ESC on a keyboard to exit if needed

print("Touch UI loaded. Please tap the screen...", flush=True)
root.mainloop()