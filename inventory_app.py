import psycopg2
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import cv2
from dotenv import load_dotenv
import threading
import pandas as pd

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates

# Ortam değişkenlerini yükle
load_dotenv()

# Veritabanı bağlantısı bilgilerini ayarlayın
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT")

class InventoryOrdersApp:
    def __init__(self, master):
        self.master = master
        self.master.title("Envanter ve Sipariş Yönetimi")
        self.detail_windows = []
        
        # Veritabanı bağlantısı
        self.connect_db()
        
        # Ana pencere boyutları
        window_width = 1200
        window_height = 800
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.master.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Notebook oluştur
        self.notebook = ttk.Notebook(master)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.inventory_frame = ttk.Frame(self.notebook)
        self.orders_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.inventory_frame, text="Envanter")
        self.notebook.add(self.orders_frame, text="Siparişler")
        
        # Önce tabloları oluştur, böylece orders tablosu mevcut olur
        self.create_tables()
        
        self.create_inventory_view()
        self.create_orders_view()
        # load_inventory artık arka planda çalışıyor
        self.load_inventory()
        self.load_orders()
        
        # Pencere kapatıldığında veritabanı bağlantısını kapat
        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)

    def connect_db(self):
        """Veritabanına bağlan"""
        try:
            self.conn = psycopg2.connect(
                host=DB_HOST,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                port=DB_PORT
            )
            print("Veritabanına başarıyla bağlandı.")
        except Exception as e:
            print(f"Veritabanı bağlantı hatası: {e}")
            messagebox.showerror("Bağlantı Hatası", 
                "Veritabanına bağlanırken bir hata oluştu.\nLütfen bağlantı ayarlarınızı kontrol edin.")
            self.master.destroy()

    def ensure_connection(self):
        """Veritabanı bağlantısının açık olduğundan emin ol"""
        try:
            cur = self.conn.cursor()
            cur.execute('SELECT 1')
            cur.close()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            print("Bağlantı koptu, yeniden bağlanılıyor...")
            self.connect_db()

    def create_tables(self):
        """Veritabanı tablolarını oluştur"""
        try:
            cur = self.conn.cursor()
            cur.execute("SET statement_timeout = 0;")
            
            # Drop existing tables in correct order (disabled to preserve existing tables)
            # cur.execute("DROP TABLE IF EXISTS order_items CASCADE;")
            # cur.execute("DROP TABLE IF EXISTS orders CASCADE;")
            # cur.execute("DROP SEQUENCE IF EXISTS order_number_seq;")
            # Sequence for auto-generating order numbers
            cur.execute("""
                CREATE SEQUENCE IF NOT EXISTS order_number_seq
                START WITH 1001
                INCREMENT BY 1
            """)
            
            # Orders table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    order_number VARCHAR(50) UNIQUE,
                    shop_name VARCHAR(100) NOT NULL,
                    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(20) DEFAULT 'Yeni',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Order items table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                    stock_code VARCHAR(50) NOT NULL,
                    product_name VARCHAR(200) NOT NULL,
                    unit VARCHAR(20) NOT NULL,
                    quantity NUMERIC(10,2) NOT NULL CHECK (quantity > 0),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Deposits table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deposits (
                    deposit_id SERIAL PRIMARY KEY,
                    stock_code VARCHAR(50) NOT NULL,
                    product_name VARCHAR(200) NOT NULL,
                    unit VARCHAR(20) NOT NULL,
                    quantity NUMERIC(10,2) NOT NULL CHECK (quantity > 0),
                    deposit_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)
            
            # Withdrawals table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    withdrawal_id SERIAL PRIMARY KEY,
                    stock_code VARCHAR(50) NOT NULL,
                    product_name VARCHAR(200) NOT NULL,
                    unit VARCHAR(20) NOT NULL,
                    quantity NUMERIC(10,2) NOT NULL CHECK (quantity > 0),
                    shop_name VARCHAR(100) NOT NULL,
                    withdrawal_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)
            
            # Order allocations table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_allocations (
                    allocation_id SERIAL PRIMARY KEY,
                    deposit_id INTEGER REFERENCES deposits(deposit_id) ON DELETE CASCADE,
                    withdrawal_id INTEGER REFERENCES withdrawals(withdrawal_id) ON DELETE CASCADE,
                    allocated_quantity NUMERIC(10,2) NOT NULL CHECK (allocated_quantity > 0),
                    allocation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            self.conn.commit()
            print("Tablolar başarıyla oluşturuldu.")
            
        except Exception as e:
            self.conn.rollback()
            print(f"Hata: {str(e)}")
            messagebox.showerror("Hata", f"Tablolar oluşturulurken bir hata oluştu: {str(e)}")
        finally:
            cur.close()

    # -------------------- Envanter Sayfası --------------------
    def create_inventory_view(self):
        # Envanter ağacı: aynı stok kodu, ürün adı, birim için grup oluşturulacak.
        self.inventory_tree = ttk.Treeview(self.inventory_frame, columns=('Stok Kodu', 'Ürün Adı', 'Birim', 'Toplam Miktar', 'Giriş Tarihi / Uyarı'), show='headings')
        for col in ('Stok Kodu', 'Ürün Adı', 'Birim', 'Toplam Miktar', 'Giriş Tarihi / Uyarı'):
            self.inventory_tree.heading(col, text=col)
        self.inventory_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Çift tıklama olayını bağla
        self.inventory_tree.bind('<Double-1>', self.on_tree_double_click)
        
        btn_frame = tk.Frame(self.inventory_frame)
        btn_frame.pack(pady=5)
        tk.Button(btn_frame, text="Giriş", command=self.open_deposit_window).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Çıkış", command=self.open_withdrawal_window).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Yenile", command=self.refresh_all).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Düzenle", command=self.edit_deposit).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Sil", command=self.delete_deposit).pack(side=tk.LEFT, padx=5)

    def load_inventory(self):
        # Artık load_inventory, arka planda çalıştırılıyor.
        print("load_inventory: Arka plan thread'i başlatılıyor.")
        threading.Thread(target=self._load_inventory_thread, daemon=True).start()

    def _load_inventory_thread(self):
        print("load_inventory_thread: Ağırlığı sorgu arka planda çalışıyor.")
        now = datetime.now()
        first_day_this_month = now.replace(day=1)
        last_day_last_month = first_day_this_month - timedelta(days=1)
        first_day_last_month = last_day_last_month.replace(day=1)
        try:
            cur = self.conn.cursor()
            # Previous query commented out:
            # cur.execute("""
            #     SELECT 
            #         d.stock_code,
            #         d.product_name,
            #         d.unit,
            #         SUM(d.quantity) AS total_qty,
            #         (
            #           SELECT COALESCE(SUM(w.quantity), 0)
            #           FROM withdrawals w
            #           WHERE w.stock_code = d.stock_code
            #             AND w.withdrawal_date >= %s
            #             AND w.withdrawal_date < %s
            #         ) AS last_month_total,
            #         MIN(d.deposit_date) AS first_deposit_date
            #     FROM deposits d
            #     WHERE d.quantity > 0
            #     GROUP BY d.stock_code, d.product_name, d.unit
            #     ORDER BY first_deposit_date ASC;
            # """, (first_day_last_month.strftime("%Y-%m-%d"), first_day_this_month.strftime("%Y-%m-%d")))
            cur.execute("""
                SELECT 
                    d.stock_code,
                    d.product_name,
                    d.unit,
                    SUM(d.quantity)
                      - COALESCE((SELECT SUM(w.quantity) FROM withdrawals w WHERE w.stock_code = d.stock_code), 0)
                    AS total_qty,
                    (
                      SELECT COALESCE(SUM(w.quantity), 0)
                      FROM withdrawals w
                      WHERE w.stock_code = d.stock_code
                        AND w.withdrawal_date >= %s
                        AND w.withdrawal_date < %s
                    ) AS last_month_total,
                    MIN(d.deposit_date) AS first_deposit_date
                FROM deposits d
                GROUP BY d.stock_code, d.product_name, d.unit
                HAVING SUM(d.quantity)
                        - COALESCE((SELECT SUM(w.quantity) FROM withdrawals w WHERE w.stock_code = d.stock_code), 0) > 0
                ORDER BY first_deposit_date ASC;
            """, (first_day_last_month.strftime("%Y-%m-%d"), first_day_this_month.strftime("%Y-%m-%d")))
            grouped_rows = cur.fetchall()
            cur.close()
            print(f"load_inventory_thread: Sorgudan {len(grouped_rows)} satır alındı.")
        except Exception as e:
            print("load_inventory_thread: Hata:", e)
            grouped_rows = []
        # Sonuçları ana GUI thread'ine aktar.
        self.master.after(0, lambda: self._update_inventory_gui(grouped_rows, first_day_last_month, first_day_this_month))

    def _update_inventory_gui(self, grouped_rows, first_day_last_month, first_day_this_month):
        print("load_inventory: GUI güncellemesi başlıyor.")
        for item in self.inventory_tree.get_children():
            self.inventory_tree.delete(item)
        self.inventory_tree.tag_configure("low_stock", foreground="red")
        for row in grouped_rows:
            stock_code, product_name, unit, total_qty, last_month_total, first_deposit_date = row
            info_text = ""
            tags = ()
            if total_qty < last_month_total:
                info_text = f"Geçen ay sipariş: {last_month_total}. Yenileme gerekli!"
                tags = ("low_stock",)
            parent_id = self.inventory_tree.insert('', tk.END, values=(stock_code, product_name, unit, total_qty, info_text), tags=tags)
            try:
                cur = self.conn.cursor()
                cur.execute("""
                    SELECT deposit_id, quantity, deposit_date 
                    FROM deposits 
                    WHERE stock_code = %s AND product_name = %s AND unit = %s AND quantity > 0
                    ORDER BY deposit_date ASC;
                """, (stock_code, product_name, unit))
                deposit_rows = cur.fetchall()
                cur.close()
            except Exception as e:
                print("Error fetching deposit details:", e)
                deposit_rows = []
            for deposit_row in deposit_rows:
                deposit_id, qty, deposit_date = deposit_row
                display_date = deposit_date.strftime("%Y-%m-%d") if isinstance(deposit_date, datetime) else deposit_date
                self.inventory_tree.insert(parent_id, tk.END, iid=str(deposit_id), values=("", "", "", qty, display_date))
        print("load_inventory: GUI güncellemesi tamamlandı.")

    def open_deposit_window(self):
        DepositWindow(self)

    def open_withdrawal_window(self):
        WithdrawalWindow(self)

    def edit_deposit(self):
        selected = self.inventory_tree.selection()
        if not selected:
            messagebox.showwarning("Seçim Yapılmadı", "Lütfen düzenlemek için bir ürün seçin.")
            return
        iid = selected[0]
        if not iid.isdigit():
            messagebox.showwarning("Hata", "Lütfen düzenlemek için bir alt kayıt seçin.")
            return
        DepositEditWindow(self, iid)

    def delete_deposit(self):
        selected = self.inventory_tree.selection()
        if not selected:
            messagebox.showwarning("Seçim Yapılmadı", "Lütfen silmek için bir ürün seçin.")
            return
        iid = selected[0]
        if not iid.isdigit():
            messagebox.showwarning("Hata", "Lütfen silmek için bir alt kayıt seçin.")
            return
        confirm = messagebox.askyesno("Onay", "Seçilen kaydı silmek istediğinize emin misiniz?")
        if confirm:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM deposits WHERE deposit_id = %s", (iid,))
            self.conn.commit()
            cur.close()
            self.load_inventory()

    def on_tree_double_click(self, event):
        item = self.inventory_tree.selection()[0]
        values = self.inventory_tree.item(item)['values']
        if values[0]:
            ProductDetailWindow(self, values[0], values[1], values[2])

    # -------------------- Sipariş Sayfası --------------------
    def create_orders_view(self):
        self.orders_tree = ttk.Treeview(self.orders_frame, 
            columns=('Sipariş No', 'Şube', 'Tarih', 'Durum', 'Ürün Sayısı', 'Toplam Miktar', 'Notlar'),
            show='headings')
        columns = {
            'Sipariş No': 100,
            'Şube': 150,
            'Tarih': 100,
            'Durum': 100,
            'Ürün Sayısı': 100,
            'Toplam Miktar': 100,
            'Notlar': 200
        }
        for col, width in columns.items():
            self.orders_tree.heading(col, text=col)
            self.orders_tree.column(col, width=width, minwidth=width)
                
        self.orders_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        def on_double_click(event):
            item = self.orders_tree.selection()
            if item:
                values = self.orders_tree.item(item[0])['values']
                if values:
                    self.show_order_detail(values[0])
        self.orders_tree.bind('<Double-1>', on_double_click)
        
        control_frame = ttk.Frame(self.orders_frame)
        control_frame.pack(pady=5)
        
        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(side=tk.LEFT, padx=10)
        
        ttk.Button(btn_frame, text="Yeni Sipariş", command=self.new_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Siparişleri Yenile", command=self.load_orders).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Sipariş Detayı", command=self.show_selected_order_detail).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Düzenle", command=self.edit_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Sil", command=self.delete_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Siparişi İçe Aktar", command=self.import_orders).pack(side=tk.LEFT, padx=5)
        
        filter_frame = ttk.Frame(control_frame)
        filter_frame.pack(side=tk.RIGHT, padx=10)
        
        ttk.Label(filter_frame, text="Şube Filtresi:").pack(side=tk.LEFT, padx=(0, 5))
        self.shop_filter_var = tk.StringVar(value="Tümü")
        self.shop_filter_combo = ttk.Combobox(filter_frame, textvariable=self.shop_filter_var, state="readonly", width=15)
        self.shop_filter_combo.pack(side=tk.LEFT)
        self.load_shop_filter()
        self.shop_filter_combo.bind('<<ComboboxSelected>>', lambda e: self.load_orders())

    def load_shop_filter(self):
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT shop_name FROM orders ORDER BY shop_name")
        shops = [row[0] for row in cur.fetchall()]
        cur.close()
        self.shop_filter_combo['values'] = ["Tümü"] + shops

    def load_orders(self):
        cur = self.conn.cursor()
        where_clause = ""
        if self.shop_filter_var.get() != "Tümü":
            where_clause = "WHERE o.shop_name = %s"
        query = f"""
            SELECT 
                o.order_number,
                o.shop_name,
                o.order_date,
                o.status,
                COUNT(oi.id) as item_count,
                COALESCE(SUM(oi.quantity), 0) as total_quantity,
                o.notes
            FROM orders o
            LEFT JOIN order_items oi ON o.id = oi.order_id
            {where_clause}
            GROUP BY o.id, o.order_number, o.shop_name, o.order_date, o.status, o.notes
            ORDER BY o.order_date DESC
        """
        try:
            if where_clause:
                cur.execute(query, (self.shop_filter_var.get(),))
            else:
                cur.execute(query)
            for item in self.orders_tree.get_children():
                self.orders_tree.delete(item)
            for order in cur.fetchall():
                order_number, shop_name, order_date, status, item_count, total_quantity, notes = order
                formatted_date = order_date.strftime('%Y-%m-%d %H:%M') if order_date else ''
                self.orders_tree.insert('', tk.END, values=(
                    order_number,
                    shop_name,
                    formatted_date,
                    status,
                    item_count,
                    total_quantity,
                    notes
                ))
        except Exception as e:
            print(f"Sipariş yükleme hatası: {e}")
            messagebox.showerror("Hata", f"Siparişler yüklenirken bir hata oluştu:\n{str(e)}")
        finally:
            cur.close()

    def new_order(self):
        OrderEntryWindow(self)

    def show_order_detail(self, order_number):
        OrderDetailWindow(self, str(order_number))

    def show_selected_order_detail(self):
        selected = self.orders_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen bir sipariş seçin!")
            return
        values = self.orders_tree.item(selected[0])['values']
        if values:
            self.show_order_detail(values[0])

    def edit_order(self):
        selected = self.orders_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen bir sipariş seçin!")
            return
        values = self.orders_tree.item(selected[0])['values']
        if values:
            messagebox.showinfo("Bilgi", "Bu özellik henüz eklenmedi.")

    def delete_order(self):
        selected = self.orders_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen bir sipariş seçin!")
            return
        values = self.orders_tree.item(selected[0])['values']
        if not values:
            return
        if messagebox.askyesno("Onay", "Bu siparişi silmek istediğinizden emin misiniz?"):
            try:
                cur = self.conn.cursor()
                cur.execute("DELETE FROM orders WHERE order_number = %s", (values[0],))
                self.conn.commit()
                messagebox.showinfo("Başarılı", "Sipariş başarıyla silindi!")
                self.load_orders()
            except Exception as e:
                self.conn.rollback()
                messagebox.showerror("Hata", f"Sipariş silinirken bir hata oluştu:\n{str(e)}")
            finally:
                cur.close()

    def import_orders(self):
        filepath = filedialog.askopenfilename(
            parent=self.master,
            title="Sipariş Dosyası Seç",
            filetypes=[
                ("CSV Dosyası", ("*.csv",)),
                ("Excel Dosyası", ("*.xls", "*.xlsx"))
            ]
        )
        if not filepath:
            return
        try:
            if filepath.lower().endswith((".xls", ".xlsx")):
                df = pd.read_excel(filepath)
            else:
                try:
                    df = pd.read_csv(filepath)
                except UnicodeDecodeError:
                    try:
                        df = pd.read_csv(filepath, encoding='ISO-8859-9')
                    except UnicodeDecodeError:
                        df = pd.read_csv(filepath, encoding='windows-1254')
            df.columns = [c.strip().lower() for c in df.columns]
            required = ["irsaliye numarası","şube adı","stok kodu","ürün adı","tarih","miktar","birim"]
            for col in required:
                if col not in df.columns:
                    messagebox.showerror("Hata", f"'{col}' sütunu bulunamadı!")
                    return
            cur = self.conn.cursor()
            for invoice, group in df.groupby("irsaliye numarası"):
                shop = group["şube adı"].iloc[0]
                date0 = pd.to_datetime(group["tarih"].iloc[0])
                cur.execute("SELECT id FROM orders WHERE order_number=%s", (invoice,))
                res = cur.fetchone()
                if res:
                    order_id = res[0]
                else:
                    cur.execute(
                        "INSERT INTO orders (order_number, shop_name, order_date) VALUES (%s, %s, %s) RETURNING id",
                        (invoice, shop, date0)
                    )
                    order_id = cur.fetchone()[0]
                for _, row in group.iterrows():
                    code = row["stok kodu"]
                    name = row["ürün adı"]
                    unit = row["birim"]
                    qty = int(row["miktar"])
                    wdate = pd.to_datetime(row["tarih"]).strftime("%Y-%m-%d %H:%M:%S")
                    cur.execute(
                        "INSERT INTO order_items (order_id, stock_code, product_name, unit, quantity) VALUES (%s, %s, %s, %s, %s)",
                        (order_id, code, name, unit, qty)
                    )
                    cur.execute(
                        "INSERT INTO withdrawals (stock_code, product_name, unit, quantity, shop_name, withdrawal_date) VALUES (%s, %s, %s, %s, %s, %s) RETURNING withdrawal_id",
                        (code, name, unit, qty, shop, wdate)
                    )
                    withdrawal_id = cur.fetchone()[0]
                    self.allocate_withdrawal(withdrawal_id, code, qty)
            self.conn.commit()
            cur.close()
            self.load_orders()
            self.load_inventory()
            messagebox.showinfo("Başarılı", "Siparişler başarıyla içe aktarıldı.")
        except Exception as e:
            messagebox.showerror("Hata", f"İçe aktarırken hata oluştu:\n{e}")

    def allocate_withdrawal(self, withdrawal_id, stock_code, withdraw_qty):
        allocations = []
        cur = self.conn.cursor()
        remaining = withdraw_qty
        cur.execute("SELECT deposit_id, quantity, deposit_date FROM deposits WHERE stock_code = %s AND quantity > 0 ORDER BY deposit_date ASC", (stock_code,))
        deposits = cur.fetchall()
        for deposit in deposits:
            deposit_id, qty, deposit_date = deposit
            if remaining <= 0:
                break
            allocate_qty = min(qty, remaining)
            # Leave the deposit record untouched; just record the allocation
            cur.execute(
                "INSERT INTO order_allocations (withdrawal_id, deposit_id, allocated_quantity) VALUES (%s, %s, %s)",
                (withdrawal_id, deposit_id, allocate_qty)
            )
            allocations.append((deposit_id, allocate_qty))
            remaining -= allocate_qty
        self.conn.commit()
        cur.close()
        if remaining > 0:
            messagebox.showwarning("Yetersiz Stok", f"Tam tahsis yapılamadı. {remaining} adet eksik.")
        return allocations

    def refresh_all(self):
        self.load_inventory()
        self.load_orders()
        for detail in self.detail_windows:
            detail.refresh_view()

    def on_closing(self):
        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.close()
        except Exception as e:
            print(f"Veritabanı kapatılırken hata oluştu: {e}")
        self.master.destroy()

# -------------------- Depo Giriş Penceresi --------------------
class DepositWindow:
    def __init__(self, app_obj):
        self.app_obj = app_obj
        self.win = tk.Toplevel()
        self.win.title("Ürün Girişi")
        self.create_form()
        self.load_product_suggestions()

    def create_form(self):
        tk.Label(self.win, text="Stok Kodu:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.stock_code_var = tk.StringVar()
        self.stock_code_cb = ttk.Combobox(self.win, textvariable=self.stock_code_var)
        self.stock_code_cb.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(self.win, text="Ürün Adı:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.product_name_var = tk.StringVar()
        self.product_name_cb = ttk.Combobox(self.win, textvariable=self.product_name_var)
        self.product_name_cb.grid(row=1, column=1, padx=5, pady=5)

        tk.Label(self.win, text="Birim:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.unit_var = tk.StringVar()
        self.unit_cb = ttk.Combobox(self.win, textvariable=self.unit_var)
        self.unit_cb.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(self.win, text="Miktar:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.quantity_entry = tk.Entry(self.win)
        self.quantity_entry.grid(row=3, column=1, padx=5, pady=5)

        tk.Label(self.win, text="Giriş Tarihi (YYYY-MM-DD HH:MM:SS):").grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        self.date_entry = tk.Entry(self.win)
        self.date_entry.grid(row=4, column=1, padx=5, pady=5)

        tk.Button(self.win, text="Kaydet", command=self.deposit_product).grid(row=5, column=0, columnspan=2, pady=10)

        self.stock_code_cb.bind("<<ComboboxSelected>>", self.on_stock_code_select)
        self.product_name_cb.bind("<<ComboboxSelected>>", self.on_product_name_select)

    def load_product_suggestions(self):
        cur = self.app_obj.conn.cursor()
        cur.execute("""
            SELECT DISTINCT stock_code, product_name, unit
            FROM (
                SELECT stock_code, product_name, unit FROM deposits
                UNION
                SELECT stock_code, product_name, unit FROM withdrawals
            ) as products
            ORDER BY product_name
        """)
        self.products = {}
        stock_codes = []
        product_names = []
        units = []
        for stock_code, product_name, unit in cur.fetchall():
            self.products[stock_code] = {'name': product_name, 'unit': unit}
            self.products[product_name] = {'code': stock_code, 'unit': unit}
            stock_codes.append(stock_code)
            product_names.append(product_name)
            if unit not in units:
                units.append(unit)
        cur.close()
        self.stock_code_cb['values'] = stock_codes
        self.product_name_cb['values'] = product_names
        self.unit_cb['values'] = sorted(units)

    def on_stock_code_select(self, event=None):
        code = self.stock_code_var.get().strip()
        if code in self.products:
            self.product_name_var.set(self.products[code]['name'])
            self.unit_var.set(self.products[code]['unit'])

    def on_product_name_select(self, event=None):
        name = self.product_name_var.get().strip()
        if name in self.products:
            self.stock_code_var.set(self.products[name]['code'])
            self.unit_var.set(self.products[name]['unit'])

    def deposit_product(self):
        stock_code = self.stock_code_var.get().strip()
        product_name = self.product_name_var.get().strip()
        unit = self.unit_var.get().strip()
        try:
            quantity = round(float(self.quantity_entry.get().strip()), 2)
        except ValueError:
            messagebox.showerror("Geçersiz Giriş", "Miktar bir sayı olmalı.")
            return
        deposit_date = self.date_entry.get().strip()
        if deposit_date == "":
            deposit_date = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
        else:
            try:
                datetime.strptime(deposit_date, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                messagebox.showerror("Geçersiz Tarih", "Tarihi 'YYYY-MM-DD HH:MM:SS' formatında giriniz.")
                return
        cur = self.app_obj.conn.cursor()
        cur.execute("INSERT INTO deposits (stock_code, product_name, unit, quantity, deposit_date) VALUES (%s, %s, %s, %s, %s)", 
                    (stock_code, product_name, unit, quantity, deposit_date))
        self.app_obj.conn.commit()
        cur.close()
        self.app_obj.load_inventory()
        self.win.destroy()

# -------------------- Ürün Çıkışı Penceresi --------------------
class WithdrawalWindow:
    def __init__(self, app_obj):
        self.app_obj = app_obj
        self.win = tk.Toplevel()
        self.win.title("Ürün Çıkışı")
        self.create_form()

    def create_form(self):
        tk.Label(self.win, text="Stok Kodu:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.stock_code_entry = tk.Entry(self.win)
        self.stock_code_entry.grid(row=0, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Miktar:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.quantity_entry = tk.Entry(self.win)
        self.quantity_entry.grid(row=1, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Şube:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.shop_entry = tk.Entry(self.win)
        self.shop_entry.grid(row=2, column=1, padx=5, pady=5)
        
        tk.Button(self.win, text="Kaydet", command=self.withdraw_product).grid(row=3, column=0, columnspan=2, pady=10)

    def withdraw_product(self):
        stock_code = self.stock_code_entry.get().strip()
        try:
            quantity = round(float(self.quantity_entry.get().strip()), 2)
        except ValueError:
            messagebox.showerror("Geçersiz Giriş", "Miktar bir sayı olmalı.")
            return
        shop_name = self.shop_entry.get().strip()
        withdrawal_date = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.app_obj.conn.cursor()
        cur.execute("SELECT product_name, unit FROM deposits WHERE stock_code = %s ORDER BY deposit_date ASC LIMIT 1", (stock_code,))
        result = cur.fetchone()
        if result:
            product_name, unit = result
        else:
            product_name = ""
            unit = ""
        cur.execute("INSERT INTO withdrawals (stock_code, product_name, unit, quantity, shop_name, withdrawal_date) VALUES (%s, %s, %s, %s, %s, %s) RETURNING withdrawal_id", 
                    (stock_code, product_name, unit, quantity, shop_name, withdrawal_date))
        withdrawal_id = cur.fetchone()[0]
        self.app_obj.conn.commit()
        allocations = self.app_obj.allocate_withdrawal(withdrawal_id, stock_code, quantity)
        details = []
        for deposit_id, alloc_qty in allocations:
            cur = self.app_obj.conn.cursor()
            cur.execute("SELECT deposit_date FROM deposits WHERE deposit_id = %s", (deposit_id,))
            deposit_date = cur.fetchone()[0]
            cur.close()
            details.append(f"{alloc_qty} adet - Giriş Tarihi: {deposit_date}")
        messagebox.showinfo("Tahsis Detayları", "\n".join(details))
        cur.close()
        self.app_obj.load_inventory()
        self.app_obj.load_orders()
        self.win.destroy()

# -------------------- Sipariş Detay Penceresi --------------------
class OrderDetailWindow:
    def __init__(self, parent, order_number):
        self.parent = parent
        self.conn = parent.conn
        self.order_number = str(order_number)
        
        self.top = tk.Toplevel(parent.master)
        self.top.title(f"Sipariş Detayı - {order_number}")
        
        window_width = 800
        window_height = 600
        screen_width = self.top.winfo_screenwidth()
        screen_height = self.top.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.top.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.top.transient(parent.master)
        self.top.grab_set()
        
        header_frame = ttk.LabelFrame(self.top, text="Sipariş Bilgileri", padding="10")
        header_frame.pack(fill=tk.X, padx=10, pady=5)
        
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT o.order_number, o.shop_name, o.order_date, o.status, o.notes
            FROM orders o
            WHERE o.order_number = %s
        """, (self.order_number,))
        
        order_info = cur.fetchone()
        if order_info:
            order_info_frame = ttk.Frame(header_frame)
            order_info_frame.pack(fill=tk.X, expand=True)
            labels = ["Sipariş No:", "Şube:", "Tarih:", "Durum:", "Notlar:"]
            for i, (label, value) in enumerate(zip(labels, order_info)):
                ttk.Label(order_info_frame, text=label).grid(row=i, column=0, padx=5, pady=2, sticky='e')
                ttk.Label(order_info_frame, text=str(value)).grid(row=i, column=1, padx=5, pady=2, sticky='w')
            
            items_frame = ttk.LabelFrame(self.top, text="Sipariş Kalemleri", padding="10")
            items_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
            
            columns = ('Stok Kodu', 'Ürün Adı', 'Birim', 'Miktar')
            self.items_tree = ttk.Treeview(items_frame, columns=columns, show='headings')
            widths = {'Stok Kodu': 100, 'Ürün Adı': 300, 'Birim': 100, 'Miktar': 100}
            for col, width in widths.items():
                self.items_tree.heading(col, text=col)
                self.items_tree.column(col, width=width, minwidth=width)
            
            scrollbar = ttk.Scrollbar(items_frame, orient=tk.VERTICAL, command=self.items_tree.yview)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            self.items_tree.configure(yscrollcommand=scrollbar.set)
            self.items_tree.pack(fill=tk.BOTH, expand=True)
            # Show FIFO allocation details on double‑click
            self.items_tree.bind('<Double-1>', self.on_item_double_click)
            
            cur.execute("""
                SELECT oi.stock_code, oi.product_name, oi.unit, oi.quantity
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                WHERE o.order_number = %s
                ORDER BY oi.id
            """, (self.order_number,))
            
            total_quantity = 0
            for item in cur.fetchall():
                self.items_tree.insert('', tk.END, values=item)
                total_quantity += item[3] if item[3] else 0
            
            total_frame = ttk.Frame(self.top, padding="10")
            total_frame.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(total_frame, text=f"Toplam Ürün Sayısı: {len(self.items_tree.get_children())} kalem").pack(side=tk.LEFT, padx=10)
            ttk.Label(total_frame, text=f"Toplam Miktar: {total_quantity}").pack(side=tk.LEFT, padx=10)
            
            button_frame = ttk.Frame(self.top)
            button_frame.pack(fill=tk.X, padx=10, pady=10)
            ttk.Button(button_frame, text="Düzenle", command=self.edit_order).pack(side=tk.LEFT, padx=5)
            ttk.Button(button_frame, text="Sil", command=self.delete_order).pack(side=tk.LEFT, padx=5)
            ttk.Button(button_frame, text="Kapat", command=self.top.destroy).pack(side=tk.RIGHT, padx=5)
        
        cur.close()

    def edit_order(self):
        self.top.destroy()
        OrderEntryWindow(self.parent, self.order_number)

    def delete_order(self):
        if messagebox.askyesno("Sipariş Sil", "Bu siparişi silmek istediğinizden emin misiniz?"):
            try:
                cur = self.conn.cursor()
                cur.execute("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE order_number = %s)", (self.order_number,))
                cur.execute("DELETE FROM orders WHERE order_number = %s", (self.order_number,))
                self.conn.commit()
                messagebox.showinfo("Başarılı", "Sipariş başarıyla silindi.")
                self.parent.load_orders()
                self.top.destroy()
            except Exception as e:
                self.conn.rollback()
                messagebox.showerror("Hata", f"Sipariş silinirken bir hata oluştu: {str(e)}")
            finally:
                cur.close()
    
    def on_item_double_click(self, event):
        selected = self.items_tree.selection()
        if not selected:
            return
        stock_code, product_name, unit, quantity = self.items_tree.item(selected[0])['values']
        try:
            cur = self.conn.cursor()
            # Find the withdrawal for this order item
            cur.execute(
                """
                SELECT w.withdrawal_id
                FROM withdrawals w
                JOIN orders o ON o.shop_name = w.shop_name
                WHERE o.order_number = %s
                  AND w.stock_code = %s
                  AND w.product_name = %s
                  AND w.unit = %s
                  AND w.quantity = %s
                ORDER BY w.withdrawal_date ASC
                LIMIT 1
                """,
                (self.order_number, stock_code, product_name, unit, quantity)
            )
            res = cur.fetchone()
            if not res:
                messagebox.showinfo("Bilgi", "Tahsis kaydı bulunamadı.")
                cur.close()
                return
            withdrawal_id = res[0]
            # Fetch allocation details
            cur.execute(
                """
                SELECT d.deposit_date, oa.allocated_quantity
                FROM order_allocations oa
                JOIN deposits d ON oa.deposit_id = d.deposit_id
                WHERE oa.withdrawal_id = %s
                """,
                (withdrawal_id,)
            )
            allocations = cur.fetchall()
            cur.close()
            if not allocations:
                messagebox.showinfo("Bilgi", "Bu kaleme tahsis yapılmamış.")
                return
            # Display each deposit date and allocated quantity
            details = [f"{qty} adet – Giriş Tarihi: {dt.strftime('%Y-%m-%d %H:%M:%S')}" for dt, qty in allocations]
            messagebox.showinfo("Tahsis Detayları", "\n".join(details))
        except Exception as e:
            messagebox.showerror("Hata", f"Tahsis detayları alınamadı:\n{e}")

# -------------------- Ürün Detay Penceresi --------------------
class ProductDetailWindow:
    def __init__(self, app_obj, stock_code, product_name, unit):
        self.app_obj = app_obj
        self.stock_code = stock_code
        self.product_name = product_name
        self.unit = unit
        self.current_filter = "Tümü"
        
        self.win = tk.Toplevel(self.app_obj.master)
        self.app_obj.detail_windows.append(self)
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        self.win.title(f"Ürün Detayı - {product_name}")
        self.win.geometry("1200x700")
        self.win.minsize(1000, 700)
        
        self.create_view()

    def create_view(self):
        title_frame = ttk.Frame(self.win)
        title_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(title_frame, text=f"Stok Kodu: {self.stock_code}", font=('Arial', 12, 'bold')).pack(side=tk.LEFT)
        ttk.Label(title_frame, text=f"Ürün: {self.product_name}", font=('Arial', 12, 'bold')).pack(side=tk.LEFT, padx=20)
        ttk.Label(title_frame, text=f"Birim: {self.unit}", font=('Arial', 12, 'bold')).pack(side=tk.LEFT)
        
        notebook = ttk.Notebook(self.win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        transactions_frame = ttk.Frame(notebook)
        notebook.add(transactions_frame, text="Son İşlemler")
        
        graph_frame = ttk.Frame(notebook)
        notebook.add(graph_frame, text="Stok Grafiği")
        
        transactions_container = ttk.Frame(transactions_frame)
        transactions_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        control_frame = ttk.Frame(transactions_container)
        control_frame.pack(fill=tk.X, pady=(0, 5))
        
        buttons_frame = ttk.Frame(control_frame)
        buttons_frame.pack(side=tk.LEFT)
        
        ttk.Button(buttons_frame, text="Düzenle", command=self.edit_transaction).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons_frame, text="Sil", command=self.delete_transaction).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons_frame, text="Yenile", command=self.refresh_view).pack(side=tk.LEFT, padx=5)
        
        filter_frame = ttk.Frame(control_frame)
        filter_frame.pack(side=tk.RIGHT)
        
        ttk.Label(filter_frame, text="Filtre:").pack(side=tk.LEFT, padx=(0, 5))
        self.filter_var = tk.StringVar(value="Tümü")
        filter_combo = ttk.Combobox(filter_frame, textvariable=self.filter_var, values=["Tümü", "Giriş", "Çıkış"], state="readonly", width=10)
        filter_combo.pack(side=tk.LEFT)
        filter_combo.bind('<<ComboboxSelected>>', lambda e: self.load_transactions())
        # Tarih aralığı için giriş
        ttk.Label(filter_frame, text="Başlangıç (YYYY-MM-DD):").pack(side=tk.LEFT, padx=(10, 5))
        self.start_date_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.start_date_var, width=12).pack(side=tk.LEFT)
        ttk.Label(filter_frame, text="Bitiş (YYYY-MM-DD):").pack(side=tk.LEFT, padx=(10, 5))
        self.end_date_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.end_date_var, width=12).pack(side=tk.LEFT)
        ttk.Button(filter_frame, text="Tarih Filtrele", command=self.load_transactions).pack(side=tk.LEFT, padx=(10, 0))
        
        self.transactions_tree = ttk.Treeview(transactions_container, 
            columns=('ID', 'Tarih', 'İşlem Tipi', 'Miktar', 'Kalan Stok', 'Detay'),
            show='headings')
        columns = {'ID': 0, 'Tarih': 150, 'İşlem Tipi': 100, 'Miktar': 100, 'Kalan Stok': 100, 'Detay': 200}
        for col, width in columns.items():
            self.transactions_tree.heading(col, text=col)
            if col == 'ID':
                self.transactions_tree.column('ID', width=0, stretch=False)
            else:
                self.transactions_tree.column(col, width=width, minwidth=width)
        self.transactions_tree.pack(fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(transactions_container, orient="vertical", command=self.transactions_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.transactions_tree.configure(yscrollcommand=scrollbar.set)
        
        self.fig = plt.Figure(figsize=(8, 4))
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.load_transactions()
        self.create_graph()

    def edit_transaction(self):
        selected = self.transactions_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen düzenlenecek işlemi seçin.")
            return
        item = self.transactions_tree.item(selected[0])
        values = item['values']
        transaction_id = values[0]
        transaction_type = values[2]
        if transaction_type == 'Giriş':
            DepositEditWindow(self.app_obj, transaction_id)
        else:
            WithdrawalEditWindow(self.app_obj, transaction_id)
        self.load_transactions()
        self.create_graph()
        self.app_obj.load_inventory()
        self.app_obj.load_orders()

    def delete_transaction(self):
        selected = self.transactions_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen silinecek işlemi seçin.")
            return
        item = self.transactions_tree.item(selected[0])
        values = item['values']
        transaction_id = values[0]
        transaction_type = values[2]
        if not messagebox.askyesno("Onay", "Bu işlemi silmek istediğinize emin misiniz?"):
            return
        cur = self.app_obj.conn.cursor()
        try:
            if transaction_type == 'Giriş':
                cur.execute("""
                    SELECT COUNT(*) FROM order_allocations 
                    WHERE deposit_id = %s
                """, (transaction_id,))
                if cur.fetchone()[0] > 0:
                    messagebox.showerror("Hata", "Bu giriş kaydına bağlı çıkış işlemleri var. Önce ilgili çıkışları silmelisiniz.")
                    return
                cur.execute("DELETE FROM deposits WHERE deposit_id = %s", (transaction_id,))
            else:
                cur.execute("DELETE FROM order_allocations WHERE withdrawal_id = %s", (transaction_id,))
                cur.execute("DELETE FROM withdrawals WHERE withdrawal_id = %s", (transaction_id,))
            self.app_obj.conn.commit()
            messagebox.showinfo("Başarılı", "İşlem başarıyla silindi.")
            self.load_transactions()
            self.create_graph()
            self.app_obj.load_inventory()
            self.app_obj.load_orders()
        except Exception as e:
            self.app_obj.conn.rollback()
            messagebox.showerror("Hata", f"İşlem silinirken bir hata oluştu: {str(e)}")
        finally:
            cur.close()

    def load_transactions(self):
        # Uygulanacak tarih aralığı
        start = None
        end = None
        try:
            if self.start_date_var.get():
                start = datetime.strptime(self.start_date_var.get(), "%Y-%m-%d")
            if self.end_date_var.get():
                end = datetime.strptime(self.end_date_var.get(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("Geçersiz Tarih", "Tarihleri 'YYYY-MM-DD' formatında giriniz.")
            return

        cur = self.app_obj.conn.cursor()
        for item in self.transactions_tree.get_children():
            self.transactions_tree.delete(item)
        selected_filter = self.filter_var.get()
        if selected_filter == "Tümü":
            query = """
                SELECT deposit_id as id, deposit_date as date, quantity, 'Giriş' as type
                FROM deposits 
                WHERE stock_code = %s AND product_name = %s AND unit = %s
                UNION ALL
                SELECT withdrawal_id as id, withdrawal_date as date, -quantity, 'Çıkış' as type
                FROM withdrawals
                WHERE stock_code = %s AND product_name = %s AND unit = %s
                ORDER BY date DESC
                LIMIT 50
            """
            params = (self.stock_code, self.product_name, self.unit,
                      self.stock_code, self.product_name, self.unit)
        elif selected_filter == "Giriş":
            query = """
                SELECT deposit_id as id, deposit_date as date, quantity, 'Giriş' as type
                FROM deposits 
                WHERE stock_code = %s AND product_name = %s AND unit = %s
                ORDER BY date DESC
                LIMIT 50
            """
            params = (self.stock_code, self.product_name, self.unit)
        else:
            query = """
                SELECT withdrawal_id as id, withdrawal_date as date, -quantity, 'Çıkış' as type
                FROM withdrawals
                WHERE stock_code = %s AND product_name = %s AND unit = %s
                ORDER BY date DESC
                LIMIT 50
            """
            params = (self.stock_code, self.product_name, self.unit)
            
        cur.execute(query, params)
        rows = cur.fetchall()
        # Tarih aralığına göre filtrele
        if start or end:
            filtered = []
            for id_, date, qty, type_ in rows:
                if start and date.date() < start.date():
                    continue
                if end and date.date() > end.date():
                    continue
                filtered.append((id_, date, qty, type_))
            rows = filtered
        cur.execute("""
            SELECT COALESCE(SUM(quantity), 0) - COALESCE((
                SELECT SUM(quantity) 
                FROM withdrawals 
                WHERE stock_code = %s AND product_name = %s AND unit = %s
            ), 0)
            FROM deposits
            WHERE stock_code = %s AND product_name = %s AND unit = %s
        """, (self.stock_code, self.product_name, self.unit,
              self.stock_code, self.product_name, self.unit))
        current_stock = cur.fetchone()[0]
        running_total = current_stock
        for id_, date, qty, type_ in rows:
            detail = f"{abs(qty)} {self.unit}"
            if type_ == 'Çıkış':
                qty = abs(qty)
                running_total += qty
            else:
                running_total -= qty
            self.transactions_tree.insert('', tk.END, values=(
                id_,
                date.strftime("%Y-%m-%d %H:%M"),
                type_,
                f"{qty} {self.unit}",
                f"{running_total} {self.unit}",
                detail
            ))
        cur.close()

    def create_graph(self):
        cur = self.app_obj.conn.cursor()
        cur.execute("""
            WITH RECURSIVE dates AS (
                SELECT CURRENT_DATE as date
                UNION ALL
                SELECT date - 1
                FROM dates
                WHERE date > CURRENT_DATE - 30
            ),
            daily_transactions AS (
                SELECT date::date as trans_date,
                    COALESCE(SUM(CASE 
                        WHEN d.quantity > 0 THEN d.quantity 
                        ELSE 0 
                    END), 0) as deposits,
                    COALESCE(SUM(CASE 
                        WHEN w.quantity > 0 THEN w.quantity 
                        ELSE 0 
                    END), 0) as withdrawals
                FROM dates 
                LEFT JOIN deposits d ON date::date = d.deposit_date::date 
                    AND d.stock_code = %s AND d.product_name = %s AND d.unit = %s
                LEFT JOIN withdrawals w ON date::date = w.withdrawal_date::date 
                    AND w.stock_code = %s AND w.product_name = %s AND w.unit = %s
                GROUP BY date::date
                ORDER BY date::date
            )
            SELECT trans_date, deposits, withdrawals
            FROM daily_transactions
            ORDER BY trans_date;
        """, (self.stock_code, self.product_name, self.unit,
              self.stock_code, self.product_name, self.unit))
        rows = cur.fetchall()
        cur.close()
        dates = [row[0] for row in rows]
        deposits = [row[1] for row in rows]
        withdrawals = [row[2] for row in rows]
        
        self.ax.clear()
        # Convert dates to matplotlib format for side-by-side bars
        date_nums = mdates.date2num(dates)
        bar_width = 0.4  # width in days
        # Plot deposits to the left and withdrawals to the right of each date
        self.ax.bar(date_nums - bar_width/2, deposits, width=bar_width, label='Giriş', alpha=0.6)
        self.ax.bar(date_nums + bar_width/2, withdrawals, width=bar_width, label='Çıkış', alpha=0.6)
        # Format x-axis for dates
        self.ax.xaxis_date()
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        self.ax.tick_params(axis='x', rotation=45)
        self.ax.set_xlabel('Tarih')
        self.ax.set_ylabel('Miktar')
        self.ax.set_title(f'{self.product_name} - Son 30 Gün Stok Hareketleri')
        self.ax.legend()
        self.fig.tight_layout()
        self.canvas.draw()

    def refresh_view(self):
        self.load_transactions()
        self.create_graph()
        self.app_obj.load_inventory()
        self.app_obj.load_orders()

    def on_close(self):
        if self in self.app_obj.detail_windows:
            self.app_obj.detail_windows.remove(self)
        self.win.destroy()

# -------------------- Depo Kayıt Düzenleme Penceresi --------------------
class DepositEditWindow:
    def __init__(self, app_obj, deposit_id):
        self.app_obj = app_obj
        self.deposit_id = deposit_id
        self.win = tk.Toplevel()
        self.win.title("Ürün Girişini Düzenle")
        self.create_form()
        self.load_data()

    def create_form(self):
        tk.Label(self.win, text="Stok Kodu:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.stock_code_entry = tk.Entry(self.win)
        self.stock_code_entry.grid(row=0, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Ürün Adı:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.product_name_entry = tk.Entry(self.win)
        self.product_name_entry.grid(row=1, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Birim:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.unit_entry = tk.Entry(self.win)
        self.unit_entry.grid(row=2, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Miktar:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.quantity_entry = tk.Entry(self.win)
        self.quantity_entry.grid(row=3, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Giriş Tarihi (YYYY-MM-DD HH:MM:SS):").grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        self.deposit_date_entry = tk.Entry(self.win)
        self.deposit_date_entry.grid(row=4, column=1, padx=5, pady=5)
        
        tk.Button(self.win, text="Güncelle", command=self.update_deposit).grid(row=5, column=0, columnspan=2, pady=10)

    def load_data(self):
        cur = self.app_obj.conn.cursor()
        cur.execute("SELECT stock_code, product_name, unit, quantity, deposit_date FROM deposits WHERE deposit_id = %s", (self.deposit_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            stock_code, product_name, unit, quantity, deposit_date = row
            self.stock_code_entry.insert(0, stock_code)
            self.product_name_entry.insert(0, product_name)
            self.unit_entry.insert(0, unit)
            self.quantity_entry.insert(0, quantity)
            self.deposit_date_entry.insert(0, deposit_date.strftime("%Y-%m-%d %H:%M:%S") if isinstance(deposit_date, datetime) else deposit_date)

    def update_deposit(self):
        stock_code = self.stock_code_entry.get().strip()
        product_name = self.product_name_entry.get().strip()
        unit = self.unit_entry.get().strip()
        try:
            quantity = int(self.quantity_entry.get().strip())
        except ValueError:
            messagebox.showerror("Geçersiz Giriş", "Miktar bir tam sayı olmalı.")
            return
        deposit_date = self.deposit_date_entry.get().strip()
        try:
            datetime.strptime(deposit_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            messagebox.showerror("Geçersiz Tarih", "Tarihi 'YYYY-MM-DD HH:MM:SS' formatında giriniz.")
            return
        cur = self.app_obj.conn.cursor()
        cur.execute("UPDATE deposits SET stock_code=%s, product_name=%s, unit=%s, quantity=%s, deposit_date=%s WHERE deposit_id = %s",
                    (stock_code, product_name, unit, quantity, deposit_date, self.deposit_id))
        self.app_obj.conn.commit()
        cur.close()
        self.app_obj.load_inventory()
        self.win.destroy()

# -------------------- Sipariş Düzenleme Penceresi (Çıkış) --------------------
class WithdrawalEditWindow:
    def __init__(self, app_obj, withdrawal_id):
        self.app_obj = app_obj
        self.withdrawal_id = withdrawal_id
        self.win = tk.Toplevel()
        self.win.title("Siparişi Düzenle")
        self.create_form()
        self.load_data()

    def create_form(self):
        tk.Label(self.win, text="Stok Kodu:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.stock_code_entry = tk.Entry(self.win)
        self.stock_code_entry.grid(row=0, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Miktar:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.quantity_entry = tk.Entry(self.win)
        self.quantity_entry.grid(row=1, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Şube:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.shop_entry = tk.Entry(self.win)
        self.shop_entry.grid(row=2, column=1, padx=5, pady=5)
        
        tk.Label(self.win, text="Çıkış Tarihi (YYYY-MM-DD HH:MM:SS):").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.withdrawal_date_entry = tk.Entry(self.win)
        self.withdrawal_date_entry.grid(row=3, column=1, padx=5, pady=5)
        
        tk.Button(self.win, text="Güncelle", command=self.update_withdrawal).grid(row=4, column=0, columnspan=2, pady=10)

    def load_data(self):
        cur = self.app_obj.conn.cursor()
        cur.execute("SELECT stock_code, quantity, shop_name, withdrawal_date FROM withdrawals WHERE withdrawal_id = %s", (self.withdrawal_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            stock_code, quantity, shop_name, withdrawal_date = row
            self.stock_code_entry.insert(0, stock_code)
            self.quantity_entry.insert(0, quantity)
            self.shop_entry.insert(0, shop_name)
            self.withdrawal_date_entry.insert(0, withdrawal_date.strftime("%Y-%m-%d %H:%M:%S") if isinstance(withdrawal_date, datetime) else withdrawal_date)

    def update_withdrawal(self):
        stock_code = self.stock_code_entry.get().strip()
        try:
            quantity = int(self.quantity_entry.get().strip())
        except ValueError:
            messagebox.showerror("Geçersiz Giriş", "Miktar bir tam sayı olmalı.")
            return
        shop_name = self.shop_entry.get().strip()
        withdrawal_date = self.withdrawal_date_entry.get().strip()
        try:
            datetime.strptime(withdrawal_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            messagebox.showerror("Geçersiz Tarih", "Tarihi 'YYYY-MM-DD HH:MM:SS' formatında giriniz.")
            return
        cur = self.app_obj.conn.cursor()
        cur.execute("SELECT deposit_id, allocated_quantity FROM order_allocations WHERE withdrawal_id = %s", (self.withdrawal_id,))
        allocations = cur.fetchall()
        for deposit_id, allocated_quantity in allocations:
            cur.execute("UPDATE deposits SET quantity = quantity + %s WHERE deposit_id = %s", (allocated_quantity, deposit_id))
        cur.execute("DELETE FROM order_allocations WHERE withdrawal_id = %s", (self.withdrawal_id,))
        cur.execute("UPDATE withdrawals SET stock_code=%s, quantity=%s, shop_name=%s, withdrawal_date=%s WHERE withdrawal_id = %s",
                    (stock_code, quantity, shop_name, withdrawal_date, self.withdrawal_id))
        self.app_obj.conn.commit()
        self.app_obj.allocate_withdrawal(self.withdrawal_id, stock_code, quantity)
        cur.close()
        self.app_obj.load_orders()
        self.app_obj.load_inventory()
        self.win.destroy()

# -------------------- Sipariş Giriş Penceresi --------------------
class OrderEntryWindow:
    def __init__(self, parent):
        self.parent = parent
        self.conn = parent.conn
        
        self.window = tk.Toplevel(parent.master)
        self.window.title("Yeni Sipariş")
        self.window.geometry("1000x600")
        self.window.transient(parent.master)
        self.window.grab_set()
        
        header_frame = ttk.LabelFrame(self.window, text="Sipariş Bilgileri", padding="10")
        header_frame.pack(fill=tk.X, padx=10, pady=5)
        
        order_num_frame = ttk.Frame(header_frame)
        order_num_frame.pack(fill=tk.X, pady=5)
        ttk.Label(order_num_frame, text="Sipariş Numarası:").pack(side=tk.LEFT, padx=5)
        self.order_number = ttk.Entry(order_num_frame, width=20)
        self.order_number.pack(side=tk.LEFT, padx=5)
        ttk.Label(order_num_frame, text="(Boş bırakılırsa otomatik atanır)").pack(side=tk.LEFT, padx=5)
        
        shop_frame = ttk.Frame(header_frame)
        shop_frame.pack(fill=tk.X, pady=5)
        ttk.Label(shop_frame, text="Şube:").pack(side=tk.LEFT, padx=5)
        self.shop_var = tk.StringVar()
        self.shop_combo = ttk.Combobox(shop_frame, textvariable=self.shop_var, width=30)
        self.shop_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(shop_frame, text="Yeni Şube Ekle", command=self.add_new_shop).pack(side=tk.LEFT, padx=5)
        
        notes_frame = ttk.Frame(header_frame)
        notes_frame.pack(fill=tk.X, pady=5)
        ttk.Label(notes_frame, text="Notlar:").pack(side=tk.LEFT, padx=5)
        self.notes = tk.Entry(notes_frame, width=50)
        self.notes.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        items_frame = ttk.LabelFrame(self.window, text="Sipariş Kalemleri", padding="10")
        items_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        form_frame = ttk.Frame(items_frame)
        form_frame.pack(fill=tk.X, pady=5)
        
        for i in range(9):
            form_frame.grid_columnconfigure(i, weight=0, minsize=60)
        form_frame.grid_columnconfigure(3, weight=1)
        
        ttk.Label(form_frame, text="Stok Kodu:").grid(row=0, column=0, padx=5, pady=5, sticky='e')
        self.stock_code_var = tk.StringVar()
        self.stock_code = ttk.Combobox(form_frame, textvariable=self.stock_code_var, width=15)
        self.stock_code.grid(row=0, column=1, padx=5, pady=5, sticky='w')
        
        ttk.Label(form_frame, text="Ürün Adı:").grid(row=0, column=2, padx=5, pady=5, sticky='e')
        self.product_name_var = tk.StringVar()
        self.product_name = ttk.Combobox(form_frame, textvariable=self.product_name_var, width=40, state="readonly")
        self.product_name.grid(row=0, column=3, padx=5, pady=5, sticky='ew')
        
        ttk.Label(form_frame, text="Birim:").grid(row=0, column=4, padx=5, pady=5, sticky='e')
        self.unit_var = tk.StringVar()
        self.unit = ttk.Combobox(form_frame, textvariable=self.unit_var, width=8, state="readonly")
        self.unit.grid(row=0, column=5, padx=5, pady=5, sticky='w')
        
        ttk.Label(form_frame, text="Miktar:").grid(row=0, column=6, padx=5, pady=5, sticky='e')
        self.quantity = ttk.Entry(form_frame, width=10)
        self.quantity.grid(row=0, column=7, padx=5, pady=5, sticky='w')
        
        add_button = ttk.Button(form_frame, text="Ekle", command=self.add_item, width=8)
        add_button.grid(row=0, column=8, padx=10, pady=5, sticky='w')
        
        self.items_tree = ttk.Treeview(items_frame, 
            columns=('Stok Kodu', 'Ürün Adı', 'Birim', 'Miktar'),
            show='headings',
            height=10)
        columns = {'Stok Kodu': 100, 'Ürün Adı': 300, 'Birim': 100, 'Miktar': 100}
        for col, width in columns.items():
            self.items_tree.heading(col, text=col)
            self.items_tree.column(col, width=width, minwidth=width)
        self.items_tree.pack(fill=tk.BOTH, expand=True)
        
        ttk.Button(items_frame, text="Seçili Ürünü Sil", command=self.remove_item).pack(pady=5)
        
        button_frame = ttk.Frame(self.window)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(button_frame, text="Kaydet", command=self.save_order).pack(side=tk.RIGHT, padx=5)
        ttk.Button(button_frame, text="İptal", command=self.window.destroy).pack(side=tk.RIGHT, padx=5)
        
        self.load_shops()
        self.load_product_suggestions()
        
        self.stock_code.bind('<KeyRelease>', self.on_stock_code_change)
        self.product_name.bind('<KeyRelease>', self.on_product_name_change)
        self.stock_code.bind('<<ComboboxSelected>>', self.on_stock_code_select)
        self.product_name.bind('<<ComboboxSelected>>', self.on_product_name_select)
        
        self.window.update_idletasks()
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() // 2) - (width // 2)
        y = (self.window.winfo_screenheight() // 2) - (height // 2)
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        
        self.order_number.focus()
        
    def add_new_shop(self):
        shop_name = simpledialog.askstring("Yeni Şube", "Şube adını giriniz:")
        if shop_name:
            shop_name = shop_name.strip()
            if shop_name:
                shops = list(self.shop_combo['values'])
                if shop_name not in shops:
                    shops.append(shop_name)
                    shops.sort()
                    self.shop_combo['values'] = shops
                    self.shop_var.set(shop_name)
                else:
                    messagebox.showwarning("Uyarı", "Bu şube zaten mevcut!")
    
    def load_product_suggestions(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT stock_code, product_name, unit
            FROM (
                SELECT stock_code, product_name, unit FROM deposits
                UNION
                SELECT stock_code, product_name, unit FROM withdrawals
            ) products
            ORDER BY product_name
        """)
        self.products = {}
        stock_codes = []
        product_names = []
        units = []
        for stock_code, product_name, unit in cur.fetchall():
            self.products[stock_code] = {'name': product_name, 'unit': unit}
            self.products[product_name] = {'code': stock_code, 'unit': unit}
            stock_codes.append(stock_code)
            product_names.append(product_name)
            if unit not in units:
                units.append(unit)
        self.stock_code['values'] = stock_codes
        self.product_name['values'] = product_names
        self.unit['values'] = sorted(units)
        cur.close()
    
    def on_stock_code_change(self, event=None):
        stock_code = self.stock_code_var.get().strip()
        if stock_code in self.products:
            product = self.products[stock_code]
            self.product_name_var.set(product['name'])
            self.unit_var.set(product['unit'])
    
    def on_product_name_change(self, event=None):
        product_name = self.product_name_var.get().strip()
        if product_name in self.products:
            product = self.products[product_name]
            self.stock_code_var.set(product['code'])
            self.unit_var.set(product['unit'])
    
    def on_stock_code_select(self, event=None):
        self.on_stock_code_change()
    
    def on_product_name_select(self, event=None):
        self.on_product_name_change()
    
    def add_item(self):
        stock_code = self.stock_code_var.get().strip()
        product_name = self.product_name_var.get().strip()
        unit = self.unit_var.get().strip()
        quantity = self.quantity.get().strip()
        if not all([stock_code, product_name, unit, quantity]):
            messagebox.showerror("Hata", "Lütfen tüm alanları doldurun!")
            return
        try:
            quantity = round(float(quantity), 2)
            if quantity <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Hata", "Miktar pozitif bir sayı olmalıdır!")
            return
        self.items_tree.insert('', tk.END, values=(stock_code, product_name, unit, quantity))
        self.stock_code_var.set("")
        self.product_name_var.set("")
        self.unit_var.set("")
        self.quantity.delete(0, tk.END)
        self.stock_code.focus()
        
    def remove_item(self):
        selected = self.items_tree.selection()
        if not selected:
            messagebox.showwarning("Uyarı", "Lütfen silinecek ürünü seçin!")
            return
        self.items_tree.delete(selected[0])
        
    def save_order(self):
        if not self.shop_var.get():
            messagebox.showerror("Hata", "Lütfen şube seçin!")
            return
        items = []
        for item_id in self.items_tree.get_children():
            items.append(self.items_tree.item(item_id)['values'])
        if not items:
            messagebox.showerror("Hata", "Lütfen en az bir ürün ekleyin!")
            return
        try:
            cur = self.conn.cursor()
            order_number = self.order_number.get().strip()
            if order_number:
                cur.execute("SELECT 1 FROM orders WHERE order_number = %s", (order_number,))
                if cur.fetchone():
                    messagebox.showerror("Hata", "Bu sipariş numarası zaten kullanımda!")
                    return
            else:
                cur.execute("SELECT nextval('order_number_seq')")
                seq_value = cur.fetchone()[0]
                order_number = f"ORD{seq_value:04d}"
            cur.execute("""
                INSERT INTO orders (order_number, shop_name, notes)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (order_number, self.shop_var.get(), self.notes.get().strip()))
            order_id = cur.fetchone()[0]
            for stock_code, product_name, unit, quantity in items:
                cur.execute("""
                    INSERT INTO order_items (order_id, stock_code, product_name, unit, quantity)
                    VALUES (%s, %s, %s, %s, %s)
                """, (order_id, stock_code, product_name, unit, quantity))
                # Create a withdrawal record linked to this order and deduct inventory
                withdrawal_date = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT INTO withdrawals (stock_code, product_name, unit, quantity, shop_name, withdrawal_date) VALUES (%s, %s, %s, %s, %s, %s) RETURNING withdrawal_id",
                    (stock_code, product_name, unit, quantity, self.shop_var.get(), withdrawal_date)
                )
                withdrawal_id = cur.fetchone()[0]
                # Allocate stock from earliest deposits
                self.parent.allocate_withdrawal(withdrawal_id, stock_code, quantity)
                self.parent.conn.commit()
                messagebox.showinfo("Başarılı", f"Sipariş başarıyla kaydedildi.\nSipariş Numarası: {order_number}")
                self.parent.load_orders()
                self.window.destroy()
        except Exception as e:
            self.parent.conn.rollback()
            messagebox.showerror("Hata", f"Sipariş kaydedilirken bir hata oluştu:\n{str(e)}")
        finally:
            cur.close()

    def load_shops(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT shop_name 
            FROM (
                SELECT shop_name FROM orders
                UNION
                SELECT shop_name FROM withdrawals
            ) shops
            ORDER BY shop_name
        """)
        shops = [row[0] for row in cur.fetchall()]
        cur.close()
        if shops:
            self.shop_combo['values'] = [""] + shops
            self.shop_combo.set("")

    def on_closing(self):
        try:
            if hasattr(self, 'conn'):
                self.conn.close()
        except:
            pass
        self.master.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = InventoryOrdersApp(root)
    root.mainloop()