import os
import re
import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import google.generativeai as genai
import requests
import fitz  # PyMuPDF
import threading
import io
import tabula


# --- ReportLab for PDF Export ---
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

# ==============================================================================
# --- BACKEND LOGIC ---
# ==============================================================================

def calculate_gst_breakdown(price, quantity, gst_rate, interstate=False):
    """Calculate GST breakdown and total price."""
    base_price = price * quantity
    gst_amount = (base_price * gst_rate) / 100
    if interstate:
        cgst, sgst, igst = 0, 0, gst_amount
    else:
        cgst, sgst, igst = gst_amount / 2, gst_amount / 2, 0
    total_amount = base_price + gst_amount
    return gst_rate, round(cgst, 2), round(sgst, 2), round(igst, 2), round(total_amount, 2), round(base_price, 2)

# ==============================================================================
# --- FRONTEND (GUI) IMPLEMENTATION ---
# ==============================================================================

class SmartGSTCalculator(tk.Tk):
    # --- Centralized Configuration ---
    CONFIG = {
        "api_key": "use your own api key,gemini-flash 2.0", 
        "google_sheet_url": "https://docs.google.com/spreadsheets/d/e/2PACX-1vS5WsxKofUeuvS9IodfMnpJVYA8EAz5QRfjylrK17jWEf5oWiACl5YtpWP2_3ImpQ/pub?gid=634683792&single=true&output=csv",
        "gst_pdf_url": "https://drive.google.com/uc?export=download&id=1d9B_cyi-P4uDt_LLHrxc2B6egTQxV7oU"
    }

    def __init__(self):
        super().__init__()
        self.title("Smart GST Calculator (Hybrid AI Edition)")
        self.geometry("1050x750")

        # --- Style Configuration ---
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame', background='#f1f1f1')
        style.configure('TLabel', background='#f1f1f1', font=('Segoe UI', 10))
        style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=6)
        style.configure('Header.TLabel', font=('Segoe UI', 16, 'bold'), foreground='#005a9e')
        style.configure("Treeview.Heading", font=('Segoe UI', 10, 'bold'))
        style.configure("Status.TLabel", font=('Segoe UI', 9), background='#e0e0e0')

        # --- Class Variables ---
        self.debounce_job = None
        self.items = []
        self.product_db = pd.DataFrame(columns=['HSN_Code', 'HSN_Description'])
        self.pdf_text_cache = None
        self.search_mode = tk.StringVar(value="name") # 'name' or 'hsn'

        self.create_widgets()

        # --- Load Data Sources on Startup in the Background ---
        self.set_status("Loading initial data...")
        threading.Thread(target=self.initialize_data_sources, daemon=True).start()

    def initialize_data_sources(self):
        """Loads both Google Sheet and PDF data."""
        self._load_google_sheet()
        self._load_and_cache_pdf()
        self.set_status("Ready")

    def _load_google_sheet(self):
        """Loads product suggestions and HSN codes from a Google Sheet."""
        self.set_status("Loading product data from Google Sheet...")
        try:
            df = pd.read_csv(self.CONFIG["google_sheet_url"])
            df['HSN_Code'] = df['HSN_Code'].astype(str)
            self.product_db = df
            print("Google Sheet database loaded successfully.")
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Google Sheet Error", f"Could not load suggestions.\nError: {err}"))

    def _load_and_cache_pdf(self):
        """Downloads the GST PDF and caches its text for rate lookups."""
        self.set_status("Downloading and caching official GST PDF...")
        try:
            response = requests.get(self.CONFIG["gst_pdf_url"], timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            
            if 'text/html' in response.headers.get('Content-Type', ''):
                raise Exception("The provided URL is for a webpage, not a direct PDF download link.")

            pdf_document = fitz.open(stream=io.BytesIO(response.content), filetype="pdf")
            self.pdf_text_cache = "".join(page.get_text() for page in pdf_document).lower()
            print("PDF content successfully loaded and cached.")
        except Exception as e:
            self.pdf_text_cache = None
            self.after(0, lambda err=e: messagebox.showerror("PDF Load Error", f"Failed to load the GST rate PDF.\n\nError: {err}"))

    def get_ai_suggestions(self, user_input):
        self.set_status(f"Getting AI suggestions for '{user_input}'...")
        try:
            genai.configure(api_key=self.CONFIG["api_key"])
            model = genai.GenerativeModel('gemini-2.0-flash')
            prompt = f"""
            You are a helpful assistant for a product search engine. For the given user input, generate a comma-separated list of related keywords. Include broader categories, synonyms, and similar items. Provide only the list.
            USER INPUT: {user_input} -> AI Output:
            """
            response = model.generate_content(prompt)
            suggestions = response.text.strip().split(',')
            all_terms = {user_input.strip().lower()}
            for s in suggestions:
                s_clean = s.strip().lower()
                if s_clean:
                    all_terms.add(s_clean)
            return list(all_terms)
        except Exception as e:
            print(f"AI Suggestion Error: {e}")
            return [user_input]

    def _extract_relevant_snippet(self, hsn_code):
        if not self.pdf_text_cache: return None
        search_terms = [hsn_code, hsn_code[:6], hsn_code[:4], hsn_code[:2]]
        for term in search_terms:
            try:
                index = self.pdf_text_cache.find(term)
                if index != -1:
                    start = max(0, index - 2000)
                    end = min(len(self.pdf_text_cache), index + 2000)
                    return self.pdf_text_cache[start:end]
            except Exception:
                continue
        return None

    def predict_gst_from_pdf_hierarchical(self, hsn_code):
        self.set_status(f"Scanning PDF for HSN {hsn_code}...")
        snippet = self._extract_relevant_snippet(hsn_code)
        if snippet is None:
            self.after(0, lambda: messagebox.showwarning("Not Found", f"HSN {hsn_code} not in PDF. Defaulting to 18%."))
            return 18.0

        self.set_status(f"AI is analyzing snippet for HSN {hsn_code}...")
        try:
            genai.configure(api_key=self.CONFIG["api_key"])
            model = genai.GenerativeModel('gemini-2.0-flash')
            prompt = f"""
            You are an expert tax data extractor. From the following text snippet from a GST document, find and calculate the total GST rate for HSN Code '{hsn_code}'.
            **Instructions:**
            1. Find the Rate: Find the rate associated with '{hsn_code}' or its parent heading.
            2. Assume CGST: Assume the 'Rate' column refers to the CGST rate.
            3. Calculate Total GST: Your primary task is to calculate the total GST by doubling the found rate (Total GST = 2 * CGST Rate).
            4. CRUCIAL EXCLUSION CHECK: You MUST check for negative keywords like `except`, `other than`, or `excluding`. If '{hsn_code}' is in an exclusion clause, the parent rate does NOT apply.
            5. Output Format: Your response MUST BE ONLY THE NUMERICAL VALUE of the **final calculated total GST rate**.
            6. Default: If you cannot determine the rate, return '18'.
            **TEXT SNIPPET TO ANALYZE:**
            ---
            {snippet}
            ---
            """
            response = model.generate_content(prompt)
            match = re.search(r'([\d\.]+)$', response.text.strip())
            cleaned_response = match.group(1) if match else "invalid"

            try:
                rate = float(cleaned_response)
                return rate if 0 <= rate <= 100 else 18.0
            except (ValueError, TypeError):
                return 18.0
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("AI Error", f"Could not predict GST rate.\nError: {err}"))
            return 18.0

    def create_widgets(self):
        # --- Main Layout ---
        status_bar_frame = ttk.Frame(self, relief='sunken', padding=(5, 2))
        status_bar_frame.pack(side='bottom', fill='x')
        self.status_label = ttk.Label(status_bar_frame, text="Initializing...", style="Status.TLabel")
        self.status_label.pack(fill='x')

        main_frame = ttk.Frame(self, padding="15")
        main_frame.pack(fill='both', expand=True)
        ttk.Label(main_frame, text="Smart GST Calculator", style='Header.TLabel').pack(pady=(0, 15))

        # --- Input Frame ---
        input_frame = ttk.LabelFrame(main_frame, text="Add New Item", padding="10")
        input_frame.pack(fill='x', pady=10)
        
        # --- Search Mode Selection ---
        search_mode_frame = ttk.Frame(input_frame)
        search_mode_frame.grid(row=0, column=0, columnspan=3, sticky='w', padx=5, pady=(0, 5))
        ttk.Label(search_mode_frame, text="Search by:").pack(side='left', anchor='center')
        ttk.Radiobutton(search_mode_frame, text="Item Name", variable=self.search_mode, value="name", command=self._update_search_mode).pack(side='left', padx=5)
        ttk.Radiobutton(search_mode_frame, text="HSN Code", variable=self.search_mode, value="hsn", command=self._update_search_mode).pack(side='left', padx=5)

        # --- Dynamic Search Entry ---
        self.item_name_label = ttk.Label(input_frame, text="Item Name:")
        self.item_name_label.grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.item_name_var = tk.StringVar()
        self.item_name_entry = ttk.Entry(input_frame, textvariable=self.item_name_var, width=30)
        self.item_name_entry.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
        self.item_name_entry.bind("<KeyRelease>", self.update_suggestions)
        self.bind("<Button-1>", self.hide_suggestions)
        self.suggestion_listbox = tk.Listbox(self, height=8, font=('Segoe UI', 10))
        self.suggestion_listbox.bind("<<ListboxSelect>>", self.on_suggestion_select)

        # --- Search button ---
        self.search_button = ttk.Button(input_frame, text="Search", command=self._trigger_name_search_threaded)
        self.search_button.grid(row=1, column=2, padx=(5,0), pady=5, sticky='w')

        # --- Other Inputs ---
        self.price_var = tk.DoubleVar()
        self.quantity_var = tk.IntVar(value=1)
        self.interstate_var = tk.BooleanVar(value=False)
        ttk.Label(input_frame, text="Price per Unit (₹):").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        ttk.Entry(input_frame, textvariable=self.price_var, width=15).grid(row=2, column=1, padx=5, pady=5, sticky='w')
        ttk.Label(input_frame, text="Quantity:").grid(row=2, column=2, padx=5, pady=5, sticky='w')
        ttk.Spinbox(input_frame, from_=1, to=1000, textvariable=self.quantity_var, width=5).grid(row=2, column=3, padx=5, pady=5, sticky='w')
        ttk.Checkbutton(input_frame, text="Interstate Sale (IGST)", variable=self.interstate_var).grid(row=3, column=0, padx=5, pady=5, sticky='w')
        self.add_item_button = ttk.Button(input_frame, text="Predict & Add Item", command=self.add_item_threaded)
        self.add_item_button.grid(row=3, column=1, columnspan=3, padx=5, pady=10, sticky='ew')
        input_frame.grid_columnconfigure(1, weight=1)

        # --- Bill Summary & Footer ---
        summary_frame = ttk.LabelFrame(main_frame, text="Bill Summary", padding="10")
        summary_frame.pack(fill='both', expand=True, pady=10)
        columns = ("item", "hsn", "price", "qty", "base_price", "gst_rate", "cgst", "sgst", "igst", "total")
        self.tree = ttk.Treeview(summary_frame, columns=columns, show="headings")
        headings = {"item": "Item Name", "hsn": "HSN Code", "price": "Unit Price", "qty": "Qty", "base_price": "Base Price", "gst_rate": "GST %", "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "total": "Total"}
        widths = {"item": 200, "hsn": 80, "price": 80, "qty": 40, "base_price": 80, "gst_rate": 50, "cgst": 70, "sgst": 70, "igst": 70, "total": 100}
        for col, head in headings.items():
            self.tree.heading(col, text=head)
            self.tree.column(col, width=widths[col], anchor='center')
        self.tree.pack(fill="both", expand=True)

        footer_frame = ttk.Frame(main_frame)
        footer_frame.pack(fill='x', pady=10)
        self.grand_total_label = ttk.Label(footer_frame, text="Grand Total: ₹0.00", style='Header.TLabel', font=('Segoe UI', 12, 'bold'), foreground="#3d8c40")
        self.grand_total_label.pack(side='left', padx=10)
        ttk.Button(footer_frame, text="Export PDF", command=self.export_pdf).pack(side='right', padx=5)
        ttk.Button(footer_frame, text="Clear Bill", command=self.clear_bill).pack(side='right')

    def _update_search_mode(self):
        """Updates the UI when the search mode radio button is changed."""
        if self.search_mode.get() == "name":
            self.item_name_label.config(text="Item Name:")
            self.search_button.grid() # Show the search button
        else:
            self.item_name_label.config(text="HSN Code:")
            self.search_button.grid_remove() # Hide the search button
        self.item_name_var.set("")
        self.hide_suggestions()

    def set_status(self, message):
        self.after(0, lambda: self.status_label.config(text=message))

    def update_suggestions(self, event=None):
        """This function is bound to KeyRelease and now ONLY works for HSN mode."""
        if self.search_mode.get() == 'hsn':
            if self.debounce_job: self.after_cancel(self.debounce_job)
            self.debounce_job = self.after(400, lambda: threading.Thread(target=self._perform_search_by_hsn, daemon=True).start())

    def _trigger_name_search_threaded(self):
        """Command for the search button, works only in 'name' mode."""
        if self.search_mode.get() == 'name':
            threading.Thread(target=self._perform_search_by_name, daemon=True).start()

    def _perform_search_by_hsn(self):
        """Performs a direct, fast search for HSN codes as the user types."""
        typed_text = self.item_name_var.get().strip()
        if not typed_text or self.product_db.empty:
            self.after(0, self.hide_suggestions)
            return

        self.set_status(f"Searching for HSN code starting with '{typed_text}'...")
        results = self.product_db[self.product_db['HSN_Code'].str.startswith(typed_text, na=False)]
        
        if results.empty:
            self.after(0, self.hide_suggestions)
            self.set_status("Ready")
            return
            
        suggestions = [
            f"{row['HSN_Code']} - {row['HSN_Description']}" 
            for _, row in results.head(20).iterrows()
        ]
        self.after(0, self.display_suggestions, suggestions)
        self.set_status("Ready")

    def _perform_search_by_name(self):
        """Performs AI keyword expansion and AI-powered ranking on button click."""
        typed_text = self.item_name_var.get().strip()
        if len(typed_text) < 2 or self.product_db.empty:
            self.after(0, self.hide_suggestions)
            return

        self.set_status(f"AI brainstorming for '{typed_text}'...")
        all_search_terms = self.get_ai_suggestions(typed_text)
        search_pattern = '|'.join(map(re.escape, all_search_terms))
        
        self.set_status("Finding all possible matches...")
        candidate_df = self.product_db[self.product_db['HSN_Description'].str.contains(search_pattern, case=False, na=False)]

        if candidate_df.empty:
            self.after(0, self.hide_suggestions)
            self.set_status("Ready")
            return

        self.set_status("AI is ranking the best matches...")
        try:
            candidate_list = [f"{row['HSN_Code']} - {row['HSN_Description']}" for _, row in candidate_df.head(25).iterrows()]
            formatted_candidates = "\n".join(candidate_list)
            
            genai.configure(api_key=self.CONFIG["api_key"])
            model = genai.GenerativeModel('gemini-2.0-flash')
            prompt = f"""
            You are a relevance ranking expert for a product search engine. A user searched for "{typed_text}".
            Below is a list of potential matches from our database.
            Your task is to re-order this list from the most relevant match to the least relevant. The best match should be first.
            You MUST return ONLY the re-ordered list, in the exact same format (`HSN - Description`) with each item on a new line. Do not add any explanation.
            POTENTIAL MATCHES:
            {formatted_candidates}
            """
            
            response = model.generate_content(prompt)
            sorted_suggestions = [line.strip() for line in response.text.strip().split('\n') if line.strip()]
            self.after(0, self.display_suggestions, sorted_suggestions)
        except Exception as e:
            print(f"AI Ranking Error: {e}")
            fallback_suggestions = [f"{row['HSN_Code']} - {row['HSN_Description']}" for _, row in candidate_df.head(15).iterrows()]
            self.after(0, self.display_suggestions, fallback_suggestions)
        self.set_status("Ready")

    def display_suggestions(self, suggestions_list):
        self.suggestion_listbox.delete(0, tk.END)
        if not suggestions_list:
            self.hide_suggestions()
            return
        for suggestion in suggestions_list:
            self.suggestion_listbox.insert(tk.END, suggestion)
        x = self.item_name_entry.winfo_rootx() - self.winfo_rootx()
        y = self.item_name_entry.winfo_rooty() - self.winfo_rooty() + self.item_name_entry.winfo_height()
        width = self.item_name_entry.winfo_width() + 150
        self.suggestion_listbox.place(x=x, y=y, width=width)

    def on_suggestion_select(self, event=None):
        if not self.suggestion_listbox.curselection(): return
        selected_item = self.suggestion_listbox.get(self.suggestion_listbox.curselection()[0])
        parts = selected_item.split(' - ', 1)
        if len(parts) == 2:
            self.item_name_var.set(parts[1]) # Always put description in the box
        else:
            self.item_name_var.set(selected_item)
        self.hide_suggestions()
        self.item_name_entry.focus()
        self.item_name_entry.icursor(tk.END)
        
    def hide_suggestions(self, event=None):
        if event and event.widget in (self.item_name_entry, self.suggestion_listbox):
            return
        self.suggestion_listbox.place_forget()
        
    def add_item_threaded(self):
        try:
            item_name = self.item_name_var.get().strip()
            price = self.price_var.get()
            if not item_name or price <= 0:
                messagebox.showerror("Input Error", "Please provide a valid Item Name and Price (>0).")
                return
        except tk.TclError:
            messagebox.showerror("Input Error", "Please enter a valid Price.")
            return
        self.add_item_button.config(state="disabled")
        self.set_status("Starting prediction...")
        threading.Thread(target=self._process_item_background, daemon=True).start()

    def _process_item_background(self):
        # --- THIS IS THE KEY FIX ---
        # Always use the final content of the entry box as the item name
        item_name = self.item_name_var.get().strip()
        price = self.price_var.get()
        quantity = self.quantity_var.get()
        
        self.after(0, self.hide_suggestions)
        hsn_code = "N/A"
        if not self.product_db.empty:
            self.set_status(f"Searching for HSN code for '{item_name}'...")
            # Use an exact match against the description to find the HSN
            results = self.product_db[self.product_db['HSN_Description'].str.lower() == item_name.lower()]
            if not results.empty:
                hsn_code = results.iloc[0]['HSN_Code']
        
        ai_rate = 18.0
        if hsn_code != "N/A":
            ai_rate = self.predict_gst_from_pdf_hierarchical(hsn_code)
        
        self.set_status("Ready")
        # Pass the corrected item_name to the confirmation dialog
        self.after(0, self._ask_user_and_commit, item_name, price, quantity, ai_rate, hsn_code)

    def _ask_user_and_commit(self, item_name, price, quantity, final_rate, hsn_code):
        user_choice = messagebox.askyesnocancel("Confirm Details",
                                                  f"Item: {item_name}\n"
                                                  f"HSN Code: {hsn_code}\n"
                                                  f"AI Suggests GST Rate: {final_rate}%\n\n"
                                                  f"Use this GST rate?")
        if user_choice is True:
            self.commit_item_to_bill(item_name, price, quantity, final_rate, hsn_code)
        elif user_choice is False:
            self.prompt_for_manual_rate(item_name, price, quantity, hsn_code)
        self.add_item_button.config(state="normal")
        
    def prompt_for_manual_rate(self, item_name, price, quantity, hsn_code):
        win = tk.Toplevel(self)
        win.title("Manual GST Rate")
        ttk.Label(win, text=f"Enter GST Rate (%) for {item_name}:").pack(padx=20, pady=10)
        rate_entry = ttk.Entry(win)
        rate_entry.pack(padx=20, pady=5)
        rate_entry.focus()
        def on_submit():
            try:
                manual_rate = float(rate_entry.get())
                if 0 <= manual_rate <= 100:
                    self.commit_item_to_bill(item_name, price, quantity, manual_rate, hsn_code)
                    win.destroy()
                else:
                    messagebox.showerror("Invalid Rate", "Rate must be between 0 and 100.", parent=win)
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter a valid number.", parent=win)
        ttk.Button(win, text="Submit", command=on_submit).pack(padx=20, pady=10)
        win.transient(self); win.grab_set(); self.wait_window(win)

    def commit_item_to_bill(self, item_name, price, quantity, gst_rate, hsn_code):
        rate, cgst, sgst, igst, total, base_price = calculate_gst_breakdown(price, quantity, gst_rate, self.interstate_var.get())
        self.items.append((item_name, price, quantity, rate, cgst, sgst, igst, total, base_price, hsn_code))
        self.update_bill_display()
        self.clear_input_fields()

    def clear_input_fields(self):
        self.item_name_var.set(""), self.price_var.set(0.0), self.quantity_var.set(1)

    def clear_bill(self):
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to clear the entire bill?"):
            self.items.clear()
            self.update_bill_display()
            self.clear_input_fields()

    def update_bill_display(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        grand_total = 0
        for item_data in self.items:
            (item, price, qty, rate, cgst, sgst, igst, total, base_price, hsn_code) = item_data
            values = (item.title(), hsn_code, f"₹{price:.2f}", qty, f"₹{base_price:.2f}", f"{rate:.1f}%", f"₹{cgst:.2f}", f"₹{sgst:.2f}", f"₹{igst:.2f}", f"₹{total:.2f}")
            self.tree.insert("", "end", values=values)
            grand_total += total
        self.grand_total_label.config(text=f"Grand Total: ₹{grand_total:.2f}")

    def export_pdf(self):
        if not self.items:
            messagebox.showwarning("Export Warning", "Cannot export an empty bill.")
            return
        filepath = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Documents", "*.pdf")], initialfile="GST_Bill.pdf")
        if not filepath: return
        try:
            doc = SimpleDocTemplate(filepath, pagesize=letter)
            styles = getSampleStyleSheet()
            story = [Paragraph("GST Bill Summary", styles['h1']), Spacer(1, 12)]
            data = [['Item', 'HSN', 'Price', 'Qty', 'Base', 'GST%', 'CGST', 'SGST', 'IGST', 'Total']]
            for item in self.items:
                data.append([item[0].title(), item[9], f"₹{item[1]:.2f}", item[2], f"₹{item[8]:.2f}", f"{item[3]:.1f}%", f"₹{item[4]:.2f}", f"₹{item[5]:.2f}", f"₹{item[6]:.2f}", f"₹{item[7]:.2f}"])
            grand_total = sum(i[7] for i in self.items)
            data.append(['GRAND TOTAL', '', '', '', '', '', '', '', '', f"₹{grand_total:.2f}"])
            table = Table(data, colWidths=[180, 60, 60, 30, 60, 40, 50, 50, 50, 70])
            table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.grey), ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke), ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('BOTTOMPADDING', (0, 0), (-1, 0), 12), ('BACKGROUND', (0, -1), (-1, -1), colors.lightgreen), ('GRID', (0, 0), (-1, -1), 1, colors.black)]))
            story.append(table)
            doc.build(story)
            messagebox.showinfo("Success", f"Bill exported successfully to {filepath}")
        except Exception as e:
            messagebox.showerror("Export Error", f"An error occurred: {e}")

if __name__ == "__main__":
    app = SmartGSTCalculator()
    app.mainloop()