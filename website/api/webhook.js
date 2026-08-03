// Vercel Serverless Function: Stripe Webhook Handler for Tradalgo.store
// Endpoint URL: https://tradalgo.store/api/webhook

const Stripe = require('stripe');

module.exports = async (req, res) => {
    if (req.method !== 'POST') {
        res.setHeader('Allow', 'POST');
        return res.status(405).json({ error: 'Method Not Allowed' });
    }

    const stripeKey = process.env.STRIPE_SECRET_KEY;
    const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET;

    // If Stripe keys are not yet configured in environment variables, respond safely
    if (!stripeKey || !webhookSecret) {
        console.log('[Stripe Webhook] Received webhook event (Environment variables pending setup)');
        return res.status(200).json({ received: true, status: 'pending_env_config' });
    }

    const stripe = new Stripe(stripeKey);
    const sig = req.headers['stripe-signature'];

    let event;

    try {
        // Raw body parsing for Stripe signature verification
        const rawBody = await getRawBody(req);
        event = stripe.webhooks.constructEvent(rawBody, sig, webhookSecret);
    } catch (err) {
        console.error(`[Stripe Webhook Error] Signature verification failed: ${err.message}`);
        return res.status(400).send(`Webhook Error: ${err.message}`);
    }

    // Handle Successful Payment Event
    if (event.type === 'checkout.session.completed') {
        const session = event.data.object;
        
        const customerEmail = session.customer_details ? session.customer_details.email : session.customer_email;
        const paymentAmount = session.amount_total / 100;
        const currency = session.currency.toUpperCase();

        console.log(`[Stripe Webhook SUCCESS] Payment of $${paymentAmount} ${currency} received from: ${customerEmail}`);
        console.log(`[Stripe Webhook SUCCESS] Transaction ID: ${session.payment_intent || session.id}`);

        // Fulfillment logic: Send download link email / grant access
        // (You can integrate SendGrid, Resend, or your email provider here)
    }

    return res.status(200).json({ received: true });
};

// Helper function to read raw body buffer for Stripe signature verification
function getRawBody(req) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        req.on('data', (chunk) => chunks.push(chunk));
        req.on('end', () => resolve(Buffer.concat(chunks)));
        req.on('error', reject);
    });
}
