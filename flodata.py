from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
import mysql.connector
from mysql.connector import Error
from datetime import datetime, timedelta
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)

def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host="localhost",
            user="root",
            password="password",
            database="ecommerce_db"
        )
        return connection
    except Error as e:    
        logging.error(f"Database connection failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to connect to the database: {str(e)}")

@app.post("/place_order")
async def place_order(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    user_id = data.get('user_id')
    items = data.get('items', [])

    if not user_id or not items:
        raise HTTPException(status_code=400, detail="User ID and items are required")

    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        ## For authenticated users only
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

        ## Handles the logic where user is not registered still  placed an order

        # Validate stock and calculate total price
        total_price = 0
        for item in items:
            sku = item['sku']
            quantity = item['quantity']

            # check if quantity is always greater than zero
            if quantity <= 0:
                raise HTTPException(status_code=400, detail="Quantity must be greater than zero")

            # Validate product and stock
            cursor.execute("SELECT stock, price FROM products WHERE sku = %s", (sku,))
            stock_info = cursor.fetchone()

            ## Handles the logic where the ordered products not in the stock OR the required quantity cannot be fullfilled

            if not stock_info:
                raise HTTPException(status_code=404, detail=f"Product with SKU {sku} not found")
            if stock_info[0] < quantity:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for product {sku}. Available: {stock_info[0]}"
                )

            # Calculate total price
            total_price += stock_info[1] * quantity

        # Inserted as status  because order hasn't delivered yet
        cursor.execute(
            "INSERT INTO orders (user_id, total_price, status, created_at) VALUES (%s, %s, %s, %s)",
            (user_id, total_price, 'pending', datetime.now())
        )
        order_id = cursor.lastrowid

        # Insert order items and update stock
        for item in items:
            sku = item['sku']
            quantity = item['quantity']
            cursor.execute(
                "INSERT INTO order_items (order_id, sku, quantity) VALUES (%s, %s, %s)",
                (order_id, sku, quantity)
            )

            ## Updated the stock after the given order
            cursor.execute(
                "UPDATE products SET stock = stock - %s WHERE sku = %s", (quantity, sku)
            )

        # Commit transaction
        connection.commit()

        # Add background task for order confirmation
        background_tasks.add_task(send_order_confirmation, user_id, order_id)

        return {"order_id": order_id, "message": "Order placed successfully", "total_price": total_price}

    except Error as e:
        connection.rollback()
        logging.error(f"Order placement failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Order placement failed")

    finally:
        cursor.close()
        connection.close()

@app.get("/get_order_status")
async def get_order_status(order_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        # Fetch order details
        cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cursor.fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Calculate estimated delivery

        estimated_delivery = order['created_at'] + timedelta(days=7)

        current_date = datetime.now()

        ## Prepare response based on the delivery date and order status
        if current_date > estimated_delivery:
            if order['status'] == 'pending':
                message = f"Delivery was expected by {estimated_delivery}, but the order is delayed. Please contact support for further assistance."
            else:  # For refunded orders
                message = f"This order was refunded. No delivery is expected."
        else:
            message = f"Delivery is expected by {estimated_delivery}."

        return {
            "order_id": order_id,
            "status": order['status'],
            "total_price": order['total_price'],
            "created_at": order['created_at'],
            "estimated_delivery": estimated_delivery
        }

    except Error as e:
        logging.error(f"Failed to fetch order status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch order status")

    finally:
        cursor.close()
        connection.close()

@app.post("/refund_order")
async def refund_order(order_id: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        # Validate order status
        cursor.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
        order_status = cursor.fetchone()

        if not order_status or order_status[0] != 'completed':
            raise HTTPException(status_code=400, detail="Order not eligible for a refund")


        cursor.execute("SELECT sku, quantity FROM order_items WHERE order_id = %s", (order_id,))
        items = cursor.fetchall()

        # Restock the items by adding it to the latest numbers present
        for sku, quantity in items:
            cursor.execute("UPDATE products SET stock = stock + %s WHERE sku = %s", (quantity, sku))

        # Update order status
        cursor.execute("UPDATE orders SET status = 'refunded' WHERE order_id = %s", (order_id,))
        connection.commit()
        return {"order_id": order_id, "message": "Order refunded successfully"}

    except Error as e:
        connection.rollback()
        logging.error(f"Refund process failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Refund process failed")

    finally:
        cursor.close()
        connection.close()

# Background task for sending order confirmation
async def send_order_confirmation(user_id: int, order_id: int):
    logging.info(f"Order confirmation sent to user {user_id} for order {order_id}")
