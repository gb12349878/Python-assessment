from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
import mysql.connector
from mysql.connector import Error
from datetime import datetime, timedelta
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)


# Database connection function
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
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")


# Place order endpoint
@app.post("/place_order")
async def place_order(order_request: OrderRequest, background_tasks: BackgroundTasks):
    connection = get_db_connection()
    ## Concurrency issues are avoided with database locking
    ### Issue: If multiple orders are placed simultaneously for same product there could be a problem in updating stock
    with connection.cursor() as cursor:
        try:
            ## For authenticated users only
            cursor.execute("SELECT 1 FROM users WHERE user_id = %s", (order_request.user_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="User not found")
            ## Handles the logic where user is not registered still  placed an order
            total_price = 0
            # Validate stock and calculate total price
            for item in order_request.items:
                cursor.execute("SELECT stock, price FROM products WHERE sku = %s", (item.sku,))
                product = cursor.fetchone()
                if not product:
                    raise HTTPException(status_code=404, detail=f"Product {item.sku} not found")
                stock, price = product

                if stock < item.quantity:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Insufficient stock for product {item.sku}. Available: {stock}"
                    )
                total_price += price * item.quantity

            # Create order
            cursor.execute(
                "INSERT INTO orders (user_id, total_price, status, created_at) VALUES (%s, %s, %s, %s)",
                (order_request.user_id, total_price, 'pending', datetime.now())
            )
            order_id = cursor.lastrowid

            # Insert order items and update stock
            for item in order_request.items:
                cursor.execute(
                    "INSERT INTO order_items (order_id, sku, quantity) VALUES (%s, %s, %s)",
                    (order_id, item.sku, item.quantity)
                )
                ## Updated the stock after the given order
                cursor.execute(
                    "UPDATE products SET stock = stock - %s WHERE sku = %s",
                    (item.quantity, item.sku)
                )

            connection.commit()

            # Add background task for order confirmation
            background_tasks.add_task(send_order_confirmation, order_request.user_id, order_id)

            return {"order_id": order_id, "message": "Order placed successfully", "total_price": total_price}

        except Error as e:
            connection.rollback()
            logging.error(f"Order placement failed: {str(e)}")
            raise HTTPException(status_code=500, detail="Order placement failed")


# Get order status endpoint
@app.get("/get_order_status")
async def get_order_status(order_id: int):
    connection = get_db_connection()
    with connection.cursor(dictionary=True) as cursor:
        try:
            cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cursor.fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            created_at = order['created_at']
            estimated_delivery = created_at + timedelta(days=7)

            ## Prepare response based on the delivery date and order status
            status_message = (
                f"Delivery is expected by {estimated_delivery}."
                if datetime.now() <= estimated_delivery
                else "Delivery delayed, contact support."
            )
            return {
                "order_id": order_id,
                "status": order['status'],
                "total_price": order['total_price'],
                "created_at": created_at,
                "estimated_delivery": estimated_delivery,
                "message": status_message
            }
        except Error as e:
            logging.error(f"Failed to fetch order status: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to fetch order status")


# Refund order endpoint
@app.post("/refund_order")
async def refund_order(order_id: int):
    connection = get_db_connection()
    with connection.cursor() as cursor:
        try:
            # Validate order status
            cursor.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
            order_status = cursor.fetchone()

            ##  Modified response that could be more informative
            if not order_status:
                raise HTTPException(status_code=404, detail="Order not found")
            if order_status[0] not in ['completed', 'pending']:
                raise HTTPException(status_code=400, detail="Order not eligible for refund")

            # Restock the items
            cursor.execute("SELECT sku, quantity FROM order_items WHERE order_id = %s", (order_id,))
            items = cursor.fetchall()
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


# Background task for sending order confirmation
async def send_order_confirmation(user_id: int, order_id: int):
    logging.info(f"Sending order confirmation to user {user_id} for order {order_id}")

